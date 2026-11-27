"""The Anthropic provider, with the SDK installed but never reaching the network."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from agentfakes import DIAGNOSIS
from evalfakes import label_yaml

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")

from anthropic.types import Message  # noqa: E402

from sickbay.agent.providers import LATENCY_KEY, ProviderError, ProviderSetupError  # noqa: E402
from sickbay.agent.providers.anthropic import (  # noqa: E402
    API_BASE_URL,
    AnthropicProvider,
    map_error,
    normalise,
)
from sickbay.cli import main  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "basic"
FAKE_KEY = "fake-key-for-tests-only"
REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def message(stop_reason: str, content: list[dict[str, Any]]) -> Any:
    return Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 300,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
    )


TOOL_TURN: list[dict[str, Any]] = [
    {"type": "thinking", "thinking": "Check units.", "signature": "c2ln"},
    {"type": "text", "text": "Looking."},
    {"type": "tool_use", "id": "toolu_1", "name": "journal_query", "input": {"limit": 5}},
]
FINAL: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(DIAGNOSIS)}]


class FakeMessages:
    def __init__(self, outer: FakeClient) -> None:
        self.outer = outer

    def create(self, **request: Any) -> Any:
        self.outer.requests.append(request)
        if self.outer.error is not None:
            raise self.outer.error
        return self.outer.script.pop(0)


class FakeClient:
    """Stands in for anthropic.Anthropic: records its constructor kwargs and requests."""

    instances: list[FakeClient] = []
    script: list[Any] = []
    error: Exception | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.requests: list[dict[str, Any]] = []
        self.messages = FakeMessages(self)
        FakeClient.instances.append(self)


@pytest.fixture
def fake_sdk_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.instances = []
    FakeClient.script = [message("tool_use", TOOL_TURN), message("end_turn", FINAL)]
    FakeClient.error = None
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    return FakeClient


def test_base_url_is_pinned_even_with_ambient_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example.invalid/")
    provider = AnthropicProvider.from_env()
    client = provider._client
    assert str(client.base_url).rstrip("/") == API_BASE_URL == "https://api.anthropic.com"
    assert client.api_key == FAKE_KEY
    assert client.max_retries == 2


def test_constructor_arguments(fake_sdk_client: type[FakeClient]) -> None:
    AnthropicProvider.from_env()
    (client,) = fake_sdk_client.instances
    assert client.kwargs == {
        "api_key": FAKE_KEY,
        "base_url": "https://api.anthropic.com",
        "max_retries": 2,
        "open_telemetry": False,
    }


@pytest.mark.parametrize("value", [None, ""])
def test_missing_key(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", value)
    with pytest.raises(ProviderSetupError, match="^set ANTHROPIC_API_KEY$"):
        AnthropicProvider.from_env()


def _incidents(tmp_path: Path) -> Path:
    root = tmp_path / "incidents"
    shutil.copytree(FIXTURE, root / "a-case" / "snapshot")
    (root / "a-case" / "label.yaml").write_bytes(label_yaml("a-case").encode())
    return root


def _agent_argv(tmp_path: Path) -> list[str]:
    out = tmp_path / "run.json"
    return [
        "eval",
        "--agent",
        "--model",
        "claude-haiku-5-5",
        "--max-usd",
        "0.5",
        "--out",
        str(out),
        "--incidents",
        str(_incidents(tmp_path)),
    ]


def test_cli_missing_key_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(_agent_argv(tmp_path)) == 2
    assert "set ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "run.json").exists()


def test_cli_sdk_not_installed_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setitem(__import__("sys").modules, "anthropic", None)
    assert main(_agent_argv(tmp_path)) == 2
    assert "pip install 'sickbay[anthropic]'" in capsys.readouterr().err


def test_the_key_never_reaches_disk(
    tmp_path: Path, fake_sdk_client: type[FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_agent_argv(tmp_path)) == 0
    result = (tmp_path / "run.json").read_bytes()
    recording = (tmp_path / "run.recording.json").read_bytes()
    assert json.loads(result)["run"]["provider"] == "anthropic"
    assert len(json.loads(recording)["cases"][0]["exchanges"]) == 2
    for data in (result, recording, capsys.readouterr().err.encode()):
        assert FAKE_KEY.encode() not in data
    # The live request is the documented body: nothing else, no sampling or fallbacks.
    (client,) = fake_sdk_client.instances
    assert set(client.requests[0]) == {
        "model",
        "max_tokens",
        "system",
        "messages",
        "tools",
        "output_config",
        "cache_control",
    }


def test_live_run_replays_identically(
    tmp_path: Path, fake_sdk_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    from replaycheck import assert_replay_reproduces

    assert main(_agent_argv(tmp_path)) == 0
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert_replay_reproduces(tmp_path / "run.json", tmp_path / "incidents", tmp_path)


def test_normalise_round_trips_sdk_messages() -> None:
    source = message("tool_use", TOOL_TURN)
    plain = normalise(source.to_dict(mode="json"), 0.5)
    assert plain.pop(LATENCY_KEY) == 0.5
    assert plain["content"] == TOOL_TURN
    assert json.loads(json.dumps(plain)) == plain
    assert Message.model_validate(plain).to_dict(mode="json") == plain


def test_complete_returns_a_plain_dict(fake_sdk_client: type[FakeClient]) -> None:
    provider = AnthropicProvider.from_env()
    out = provider.complete({"model": "claude-haiku-5-5"})
    latency = out.pop(LATENCY_KEY)
    assert isinstance(latency, float) and latency >= 0 and round(latency, 3) == latency
    assert out["content"] == TOOL_TURN and out["stop_reason"] == "tool_use"


def _status(cls: Any, code: int) -> Any:
    body = {"type": "error", "error": {"type": "x", "message": "m"}}
    return cls("boom", response=httpx2.Response(code, request=REQ), body=body)


@pytest.mark.parametrize(
    ("make", "retryable"),
    [
        (lambda: _status(anthropic.BadRequestError, 400), False),
        (lambda: _status(anthropic.AuthenticationError, 401), False),
        (lambda: _status(anthropic.PermissionDeniedError, 403), False),
        (lambda: _status(anthropic.NotFoundError, 404), False),
        (lambda: _status(anthropic.UnprocessableEntityError, 422), False),
        (lambda: _status(anthropic.RateLimitError, 429), True),
        (lambda: _status(anthropic.InternalServerError, 500), True),
        (lambda: _status(anthropic.InternalServerError, 529), True),
        (lambda: _status(anthropic.APIStatusError, 408), True),
        (lambda: anthropic.APIConnectionError(request=REQ), True),
        (lambda: anthropic.APITimeoutError(REQ), True),
        (lambda: anthropic.AnthropicError("other"), False),
    ],
)
def test_sdk_errors_map_to_provider_errors(make: Any, retryable: bool) -> None:
    error = map_error(make())
    assert isinstance(error, ProviderError) and error.retryable is retryable
    assert FAKE_KEY not in error.message


def test_complete_raises_provider_error(fake_sdk_client: type[FakeClient]) -> None:
    fake_sdk_client.error = _status(anthropic.BadRequestError, 400)
    provider = AnthropicProvider.from_env()
    with pytest.raises(ProviderError) as exc:
        provider.complete({"model": "claude-haiku-5-5"})
    assert exc.value.retryable is False and "HTTP 400" in exc.value.message
    assert exc.value.__cause__ is None


def test_cli_provider_error_exits_4(
    tmp_path: Path, fake_sdk_client: type[FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    fake_sdk_client.error = _status(anthropic.AuthenticationError, 401)
    assert main(_agent_argv(tmp_path)) == 4
    result = json.loads((tmp_path / "run.json").read_bytes())
    (case,) = result["cases"]
    assert case["outcome"] == "error" and "HTTP 401" in case["detail"]
    assert (tmp_path / "run.recording.json").is_file()
    assert FAKE_KEY not in capsys.readouterr().err


def test_the_real_sdk_sends_the_request_body_unchanged() -> None:
    """The real client over an in-memory transport: a turn-2 agent request (strict tools,
    a thinking block in the history) goes out byte-for-byte as built, to the pinned host."""
    from agentfakes import FakeProvider, final, thinking, tool_turn, tool_use

    from sickbay.agent.loop import AgentPredictor
    from sickbay.agent.pricing import Budget
    from sickbay.snapshot import Snapshot

    fake = FakeProvider(
        [tool_turn(thinking(), tool_use("toolu_1", "list_units", {"limit": 5})), final()]
    )
    AgentPredictor(fake, "claude-sonnet-5-5", "high", Budget(None)).predict(Snapshot.open(FIXTURE))
    body = fake.requests[1]
    reply = message("end_turn", FINAL).to_dict(mode="json")
    seen: list[Any] = []

    def handler(request: Any) -> Any:
        seen.append(request)
        return httpx2.Response(200, json=reply)

    client = anthropic.Anthropic(
        api_key=FAKE_KEY,
        base_url=API_BASE_URL,
        max_retries=0,
        open_telemetry=False,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    out = AnthropicProvider(client).complete(body)
    (request,) = seen
    assert request.method == "POST" and str(request.url) == "https://api.anthropic.com/v1/messages"
    assert json.loads(request.content) == body
    assert "anthropic-beta" not in request.headers
    assert out.pop(LATENCY_KEY) >= 0 and out == reply
