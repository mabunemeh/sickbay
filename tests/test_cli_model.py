"""Model runs through the CLI with a fake provider: recording, replay, budget, errors."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from agentfakes import FailingProvider, FakeProvider, final, tool_turn, tool_use, usage
from evalfakes import label_yaml
from replaycheck import assert_replay_reproduces, committed_results, without_created_at

from sickbay import cli
from sickbay.agent.pricing import PRICES_AS_OF
from sickbay.agent.providers import ProviderError, load_recording
from sickbay.cli import main
from sickbay.evals.runner import NOT_STARTED

FIXTURE = Path(__file__).parent / "fixtures" / "basic"
REPO = Path(__file__).resolve().parent.parent
HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"


def make_incidents(root: Path, ids: tuple[str, ...] = ("a-case",)) -> Path:
    for incident in ids:
        shutil.copytree(FIXTURE, root / incident / "snapshot")
        (root / incident / "label.yaml").write_bytes(label_yaml(incident).encode())
    return root


def use_provider(monkeypatch: pytest.MonkeyPatch, provider: Any) -> None:
    monkeypatch.setattr(cli, "_make_provider", lambda name: provider)


def agent_turns() -> list[dict[str, Any]]:
    return [
        tool_turn(tool_use("t1", "journal_query", {}), tool_use("t2", "list_units", {})),
        final(),
    ]


def run_agent(tmp_path: Path, *extra: str, ids: tuple[str, ...] = ("a-case",)) -> tuple[int, Path]:
    incidents = make_incidents(tmp_path / "incidents", ids)
    out = tmp_path / "agent.json"
    argv = ["eval", "--agent", "--model", SONNET, "--max-usd", "1", "--out", str(out)]
    return main([*argv, "--incidents", str(incidents), *extra]), out


def test_agent_run_writes_result_and_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeProvider(agent_turns() * 2)
    use_provider(monkeypatch, fake)
    code, out = run_agent(tmp_path, "--repeats", "2")
    assert code == 0
    result = json.loads(out.read_bytes())
    assert result["result_schema"] == 2
    run = result["run"]
    assert {k: v for k, v in run.items() if k != "created_at"} == {
        "predictor": "agent",
        "provider": "fake",
        "model": SONNET,
        "effort": "high",  # sonnet's default, sent explicitly
        "prompt_version": "agent-v1",
        "repeats": 2,
        "max_usd": 1.0,
        "prices_as_of": PRICES_AS_OF,
        "spent_usd": 0.012,
    }
    assert all(r["output_config"]["effort"] == "high" for r in fake.requests)
    case = result["cases"][0]
    assert case["outcome"] == "ok" and case["detail"] is None
    assert case["scores"]["grounding"] == 1.0
    # 2 calls x (1000 in x $2 + 100 out x $10) = 6000 micro-dollars per case
    assert case["usage"] == {
        "input_tokens": 2000,
        "output_tokens": 200,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "api_calls": 2,
        "cost_usd": 0.006,
        "latency_s": 0.5,
    }
    assert result["summary"]["cost_usd"] == 0.012
    err = capsys.readouterr().err.strip()
    assert err == (
        "agent claude-sonnet-5-5: category 2/2, component 0/2, grounding 1.000 (n=2), $0.0120"
    )
    recording = load_recording(tmp_path / "agent.recording.json")
    assert {k: v for k, v in recording.items() if k != "cases"} == {
        "recording_schema": 1,
        "result_file": "agent.json",
        "predictor": "agent",
        "provider": "fake",
        "model": SONNET,
        "effort": "high",
        "prompt_version": "agent-v1",
        "max_usd": 1.0,
        "repeats": 2,
    }
    assert [(c["incident"], c["repeat"], len(c["exchanges"])) for c in recording["cases"]] == [
        ("a-case", 0, 2),
        ("a-case", 1, 2),
    ]
    exchange = recording["cases"][0]["exchanges"][0]
    assert set(exchange) == {"request_sha256", "response", "latency_s"}
    assert "_latency_s" not in exchange["response"] and exchange["latency_s"] == 0.25
    data = (tmp_path / "agent.recording.json").read_bytes()
    assert b"\r" not in data and data.endswith(b"}\n")
    assert data == (json.dumps(json.loads(data), sort_keys=True, indent=2) + "\n").encode()


def test_default_effort_haiku_is_medium(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProvider(agent_turns())
    use_provider(monkeypatch, fake)
    incidents = make_incidents(tmp_path / "i")
    out = tmp_path / "r.json"
    argv = ["eval", "--agent", "--model", HAIKU, "--max-usd", "0.5", "--out", str(out)]
    assert main([*argv, "--incidents", str(incidents)]) == 0
    assert json.loads(out.read_bytes())["run"]["effort"] == "medium"
    assert fake.requests[0]["output_config"]["effort"] == "medium"


def test_effort_flag_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProvider(agent_turns())
    use_provider(monkeypatch, fake)
    code, out = run_agent(tmp_path, "--effort", "low")
    assert code == 0 and json.loads(out.read_bytes())["run"]["effort"] == "low"
    assert fake.requests[0]["output_config"]["effort"] == "low"


def test_recorded_run_replays_identically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_provider(monkeypatch, FakeProvider(agent_turns() * 4))
    code, out = run_agent(tmp_path, "--repeats", "2", ids=("a-case", "b-case"))
    assert code == 0
    use_provider(monkeypatch, None)  # replay must not build a provider
    assert_replay_reproduces(out, tmp_path / "incidents", tmp_path)


def test_dump_run_replays_identically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_provider(monkeypatch, FakeProvider([final()]))
    incidents = make_incidents(tmp_path / "incidents")
    out = tmp_path / "dump.json"
    argv = ["eval", "--baseline", "dump", "--model", HAIKU, "--max-usd", "0.25"]
    assert main([*argv, "--out", str(out), "--incidents", str(incidents)]) == 0
    result = json.loads(out.read_bytes())
    assert result["run"]["predictor"] == "dump" and result["run"]["prompt_version"] == "dump-v1"
    assert result["cases"][0]["outcome"] == "ok"
    assert_replay_reproduces(out, incidents, tmp_path)


def test_a_modified_prompt_is_a_replay_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_provider(monkeypatch, FakeProvider(agent_turns()))
    code, out = run_agent(tmp_path)
    assert code == 0
    import sickbay.agent.loop as loop

    real = loop.load_prompt
    monkeypatch.setattr(loop, "load_prompt", lambda: (real()[0] + " Be brief.", real()[1]))
    replayed = tmp_path / "replayed.json"
    argv = ["eval", "--replay", str(tmp_path / "agent.recording.json"), "--out", str(replayed)]
    assert main([*argv, "--incidents", str(tmp_path / "incidents")]) == 3
    assert not replayed.exists()
    err = capsys.readouterr().err
    assert "replay mismatch: incident a-case repeat 0 exchange 0" in err


def test_replay_with_an_edited_response_is_a_mismatch_at_the_next_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_provider(monkeypatch, FakeProvider(agent_turns()))
    assert run_agent(tmp_path)[0] == 0
    path = tmp_path / "agent.recording.json"
    recording = json.loads(path.read_bytes())
    recording["cases"][0]["exchanges"][0]["response"]["content"][0]["input"] = {"limit": 5}
    path.write_text(json.dumps(recording))
    replayed = tmp_path / "replayed.json"
    argv = ["eval", "--replay", str(path), "--out", str(replayed)]
    assert main([*argv, "--incidents", str(tmp_path / "incidents")]) == 3
    assert "incident a-case repeat 0 exchange 1" in capsys.readouterr().err
    assert not replayed.exists()


def test_replay_of_other_incidents_is_a_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_provider(monkeypatch, FakeProvider(agent_turns()))
    assert run_agent(tmp_path)[0] == 0
    other = make_incidents(tmp_path / "other", ("z-case",))
    replayed = tmp_path / "replayed.json"
    argv = ["eval", "--replay", str(tmp_path / "agent.recording.json"), "--out", str(replayed)]
    assert main([*argv, "--incidents", str(other)]) == 3
    assert "incident z-case repeat 0 is not in the recording" in capsys.readouterr().err
    assert not replayed.exists()


def test_replay_with_fewer_incidents_is_a_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_provider(monkeypatch, FakeProvider(agent_turns() * 2))
    assert run_agent(tmp_path, ids=("a-case", "b-case"))[0] == 0
    shutil.rmtree(tmp_path / "incidents" / "b-case")
    replayed = tmp_path / "replayed.json"
    argv = ["eval", "--replay", str(tmp_path / "agent.recording.json"), "--out", str(replayed)]
    assert main([*argv, "--incidents", str(tmp_path / "incidents")]) == 3
    assert "b-case repeat 0 is recorded but was not run" in capsys.readouterr().err
    assert not replayed.exists()


def test_budget_hit_mid_run_exits_5_and_writes_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Each call costs $0.003 (1000 in, 100 out on sonnet). Cap $0.0075: a-case runs two
    # calls ($0.006); b-case makes one ($0.009), then the check before its second call fails.
    fake = FakeProvider(agent_turns() * 3)
    use_provider(monkeypatch, fake)
    incidents = make_incidents(tmp_path / "incidents", ("a-case", "b-case", "c-case"))
    out = tmp_path / "agent.json"
    argv = ["eval", "--agent", "--model", SONNET, "--max-usd", "0.0075", "--out", str(out)]
    assert main([*argv, "--incidents", str(incidents)]) == 5
    assert len(fake.requests) == 3
    result = json.loads(out.read_bytes())
    cases = result["cases"]
    assert [(c["incident"], c["outcome"]) for c in cases] == [
        ("a-case", "ok"),
        ("b-case", "budget_exceeded"),
        ("c-case", "budget_exceeded"),
    ]
    assert [c["call_id"] for c in cases[1]["trace"]] == ["c1", "c2"]
    assert cases[1]["usage"]["api_calls"] == 1 and cases[1]["detail"].startswith("spent $0.009")
    assert cases[2]["trace"] == [] and cases[2]["detail"] == NOT_STARTED
    assert cases[2]["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "api_calls": 0,
        "cost_usd": 0.0,
        "latency_s": 0.0,
    }
    assert cases[2]["scores"]["category"] is False
    assert result["summary"]["outcomes"]["budget_exceeded"] == 2
    assert result["run"]["spent_usd"] == 0.009 and result["summary"]["cost_usd"] == 0.009
    recording = load_recording(tmp_path / "agent.recording.json")
    assert [(c["incident"], len(c["exchanges"])) for c in recording["cases"]] == [
        ("a-case", 2),
        ("b-case", 1),
    ]
    assert "budget cap hit" in capsys.readouterr().err


def test_provider_error_exits_4_with_the_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    exc = ProviderError("BadRequestError (HTTP 400): output_config.format: bad schema", False)
    use_provider(monkeypatch, FailingProvider(agent_turns() * 3, fail_at=2, exc=exc))
    incidents = make_incidents(tmp_path / "incidents", ("a-case", "b-case", "c-case"))
    out = tmp_path / "agent.json"
    argv = ["eval", "--agent", "--model", SONNET, "--max-usd", "1", "--out", str(out)]
    assert main([*argv, "--incidents", str(incidents)]) == 4
    result = json.loads(out.read_bytes())
    assert [(c["incident"], c["outcome"]) for c in result["cases"]] == [
        ("a-case", "ok"),
        ("b-case", "error"),
    ]
    assert result["cases"][1]["detail"] == exc.message
    assert result["summary"]["cases"] == 2 and result["summary"]["outcomes"]["error"] == 1
    recording = load_recording(tmp_path / "agent.recording.json")
    assert [(c["incident"], len(c["exchanges"])) for c in recording["cases"]] == [
        ("a-case", 2),
        ("b-case", 0),
    ]
    assert "provider error" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--agent", "--max-usd", "1", "--out", "OUT"], "--model is required"),
        (["--agent", "--model", SONNET, "--out", "OUT"], "--max-usd is required"),
        (["--agent", "--model", SONNET, "--max-usd", "1"], "--out is required"),
        (["--baseline", "dump", "--model", SONNET, "--out", "OUT"], "--max-usd is required"),
        (["--agent", "--model", "claude-nope", "--max-usd", "1", "--out", "OUT"], "no price for"),
        (["--agent", "--model", SONNET, "--max-usd", "1", "--out", "NODIR"], "not a directory"),
        (["--agent", "--model", SONNET, "--max-usd", "1", "--out", "OUT", "--dry-run"], "dry-run"),
        (["--baseline", "keyword", "--model", SONNET], "--model: for model runs only"),
        (["--baseline", "keyword", "--repeats", "2"], "--repeats: for model runs only"),
        (["--baseline", "dump", "--dry-run", "--out", "OUT", "--max-usd", "1"], "--max-usd"),
        (["--replay", "REC"], "--out is required for --replay"),
        (["--replay", "REC", "--out", "OUT", "--model", SONNET], "not with --replay"),
    ],
)
def test_model_usage_errors_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    fake = FakeProvider([])
    use_provider(monkeypatch, fake)
    incidents = make_incidents(tmp_path / "incidents")
    paths = {"OUT": tmp_path / "o.json", "NODIR": tmp_path / "x" / "o.json", "REC": tmp_path}
    args = [str(paths[a]) if a in paths else a for a in argv]
    assert main(["eval", "--incidents", str(incidents), *args]) == 2
    assert message in capsys.readouterr().err
    assert fake.requests == [] and not (tmp_path / "o.json").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["--agent", "--max-usd", "0"],
        ["--agent", "--max-usd", "-1"],
        ["--agent", "--max-usd", "nan"],
        ["--agent", "--max-usd", "inf"],
        ["--agent", "--max-usd", "five"],
        ["--agent", "--repeats", "0"],
        ["--agent", "--effort", "extreme"],
        ["--agent", "--baseline", "dump"],
        ["--agent", "--replay", "x.recording.json"],
        ["--agent", "--provider", "openai"],
    ],
)
def test_bad_flag_values_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["eval", *argv])
    assert exc.value.code == 2


def test_replay_unreadable_recording_exits_3(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    bad = tmp_path / "bad.recording.json"
    for content in (b"{", b"[]", b'{"recording_schema": 2}'):
        bad.write_bytes(content)
        assert main(["eval", "--replay", str(bad), "--out", str(out)]) == 3
    assert main(["eval", "--replay", str(tmp_path / "missing.json"), "--out", str(out)]) == 3
    assert not out.exists()


def test_replay_unknown_predictor_or_prompt_version_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_provider(monkeypatch, FakeProvider(agent_turns()))
    assert run_agent(tmp_path)[0] == 0
    path = tmp_path / "agent.recording.json"
    original = json.loads(path.read_bytes())
    out = tmp_path / "r.json"
    incidents = str(tmp_path / "incidents")
    for change in ({"predictor": "keyword"}, {"prompt_version": "agent-v0"}):
        path.write_text(json.dumps({**original, **change}))
        assert (
            main(["eval", "--replay", str(path), "--out", str(out), "--incidents", incidents]) == 3
        )
    path.write_text(json.dumps({**original, "model": "claude-nope"}))
    assert main(["eval", "--replay", str(path), "--out", str(out), "--incidents", incidents]) == 2
    assert not out.exists()


def test_bad_incidents_on_a_model_run_exit_3_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeProvider(agent_turns())
    use_provider(monkeypatch, fake)
    incidents = make_incidents(tmp_path / "incidents", ("a-case", "b-case"))
    (incidents / "b-case" / "snapshot" / "commands" / "df.txt").write_bytes(b"tampered")
    out = tmp_path / "agent.json"
    argv = ["eval", "--agent", "--model", SONNET, "--max-usd", "1", "--out", str(out)]
    assert main([*argv, "--incidents", str(incidents)]) == 3
    assert fake.requests == []  # every snapshot is verified before the first call
    assert not out.exists() and not (tmp_path / "agent.recording.json").exists()


def test_keyword_summary_line_has_no_cost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    incidents = make_incidents(tmp_path / "incidents")
    out = tmp_path / "k.json"
    assert (
        main(["eval", "--baseline", "keyword", "--incidents", str(incidents), "--out", str(out)])
        == 0
    )
    assert "$" not in capsys.readouterr().err
    run = json.loads(out.read_bytes())["run"]
    assert (run["provider"], run["max_usd"], run["prices_as_of"], run["spent_usd"]) == (
        None,
        None,
        None,
        0.0,
    )


@pytest.mark.parametrize("result", committed_results(REPO / "results"), ids=lambda p: p.name)
def test_committed_recordings_replay_byte_for_byte(result: Path, tmp_path: Path) -> None:
    assert_replay_reproduces(result, REPO / "incidents", tmp_path)


def test_without_created_at() -> None:
    assert without_created_at(b'{"created_at": "2026-01-01T00:00:00Z"}') == b'{"created_at": ""}'


def test_replay_edge_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_provider(monkeypatch, FakeProvider(agent_turns()))
    assert run_agent(tmp_path)[0] == 0
    rec = str(tmp_path / "agent.recording.json")
    out = str(tmp_path / "again.json")
    assert main(["eval", "--replay", rec, "--out", out, "--dry-run"]) == 2
    assert main(["eval", "--replay", rec, "--out", out, "--incidents", str(tmp_path / "no")]) == 3
    incidents = str(tmp_path / "incidents")
    assert main(["eval", "--replay", rec, "--out", str(tmp_path), "--incidents", incidents]) == 2
    with pytest.raises(SystemExit):
        main(["eval", "--agent", "--repeats", "two"])


def test_budget_capped_run_replays_with_exit_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_provider(monkeypatch, FakeProvider(agent_turns() * 3))
    incidents = make_incidents(tmp_path / "incidents", ("a-case", "b-case", "c-case"))
    out = tmp_path / "agent.json"
    argv = ["eval", "--agent", "--model", SONNET, "--max-usd", "0.0075", "--out", str(out)]
    assert main([*argv, "--incidents", str(incidents)]) == 5
    use_provider(monkeypatch, None)
    assert_replay_reproduces(out, incidents, tmp_path)  # expects exit 5, identical bytes


@pytest.mark.parametrize("which", ["result", "recording"])
def test_out_that_is_a_directory_exits_2_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    fake = FakeProvider(agent_turns())
    use_provider(monkeypatch, fake)
    out = tmp_path / "agent.json"
    (out if which == "result" else tmp_path / "agent.recording.json").mkdir()
    code, _ = run_agent(tmp_path)
    assert code == 2 and fake.requests == []


def test_recording_is_written_before_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "agent.json"

    class Blocking(FakeProvider):
        """Makes --out unwritable after the checks, as a full disk or a race would."""

        def complete(self, request: dict[str, Any]) -> dict[str, Any]:
            out.mkdir(exist_ok=True)
            return super().complete(request)

    use_provider(monkeypatch, Blocking(agent_turns()))
    code, _ = run_agent(tmp_path)
    assert code == 2 and out.is_dir()
    recording = load_recording(tmp_path / "agent.recording.json")
    assert len(recording["cases"][0]["exchanges"]) == 2  # the paid exchanges survive


def test_totals_sum_unrounded_case_costs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # haiku, 1000 in x $0.10 + 559 out x $0.50 = 379.5 micro-dollars per case (one call).
    # Each case rounds to 6 places on its own; the totals round the exact sum, 759.
    turn = final(usage=usage(1000, 559))
    use_provider(monkeypatch, FakeProvider([turn, turn], latency=0.12345))
    incidents = make_incidents(tmp_path / "incidents", ("a-case", "b-case"))
    out = tmp_path / "agent.json"
    argv = ["eval", "--agent", "--model", HAIKU, "--max-usd", "1", "--out", str(out)]
    assert main([*argv, "--incidents", str(incidents)]) == 0
    result = json.loads(out.read_bytes())
    assert [c["usage"]["cost_usd"] for c in result["cases"]] == [0.00038, 0.00038]
    assert [c["usage"]["latency_s"] for c in result["cases"]] == [0.123, 0.123]
    assert result["summary"]["cost_usd"] == result["run"]["spent_usd"] == 0.000759
