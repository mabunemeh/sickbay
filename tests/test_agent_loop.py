"""The agent loop and the dump predictor over scripted fake providers (no network)."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from agentfakes import (
    DIAGNOSIS,
    FakeProvider,
    final,
    response,
    thinking,
    tool_turn,
    tool_use,
    usage,
)

from sickbay.agent.loop import MAX_TURNS, PROMPT_VERSION, AgentPredictor, load_prompt
from sickbay.agent.pricing import Budget, UnknownPrice
from sickbay.agent.providers import ProviderError, canonical_json
from sickbay.agent.request import MAX_TOKENS
from sickbay.agent.tools import api_tools
from sickbay.evals.baselines.dump import DumpPredictor, build_dump, prompt_text
from sickbay.evals.diagnosis import DIAGNOSIS_JSON_SCHEMA
from sickbay.evals.predictor import AbortRun
from sickbay.snapshot import Snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "basic"
MODEL = "claude-sonnet-5-5"
REQUEST_KEYS = {
    "model",
    "max_tokens",
    "system",
    "messages",
    "tools",
    "output_config",
    "cache_control",
}


def view() -> Snapshot:
    return Snapshot.open(FIXTURE)


def agent(provider: Any, budget: Budget | None = None, effort: str = "high") -> AgentPredictor:
    return AgentPredictor(provider, MODEL, effort, budget or Budget(None))


def three_turns() -> list[dict[str, Any]]:
    turn1 = tool_turn(
        thinking(),
        {"type": "text", "text": "Checking the journal and the units."},
        tool_use("toolu_1", "journal_query", {}),
        tool_use("toolu_2", "list_units", {"state": "failed"}),
        usage=usage(1000, 200, cache_write=3000),
    )
    turn2 = tool_turn(
        thinking("Now the unit."),
        tool_use("toolu_3", "unit_status", {"unit": "ledger\\x2dapi.service"}),
        usage=usage(500, 100, cache_read=4000),
    )
    return [turn1, turn2, final(usage=usage(800, 300, cache_read=4500))]


def test_network_guard_is_active() -> None:
    with pytest.raises(OSError, match="blocked"):
        socket.create_connection(("127.0.0.1", 9))
    with socket.socket() as sock, pytest.raises(OSError, match="blocked"):
        sock.connect(("127.0.0.1", 9))


def test_three_turn_run_with_parallel_tool_use() -> None:
    script = three_turns()
    fake = FakeProvider(script)
    pred = agent(fake).predict(view())
    assert pred.outcome == "ok" and pred.detail is None
    assert pred.diagnosis is not None and pred.diagnosis.to_json() == DIAGNOSIS
    assert len(fake.requests) == 3
    assert [c["call_id"] for c in pred.trace] == ["c1", "c2", "c3"]
    assert [c["tool"] for c in pred.trace] == ["journal_query", "list_units", "unit_status"]

    second = fake.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    # ONE user message with every tool_result, in block order, citing its call id.
    results = second[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_1", "toolu_2"]
    assert all(r["type"] == "tool_result" and r["is_error"] is False for r in results)
    bodies = [json.loads(r["content"]) for r in results]
    assert [b["call_id"] for b in bodies] == ["c1", "c2"]
    assert bodies[0]["result"] == pred.trace[0]["result"]
    third = fake.requests[2]["messages"]
    assert len(third) == 5 and third[:3] == second  # append-only history
    assert json.loads(third[4]["content"][0]["content"])["call_id"] == "c3"


def test_thinking_blocks_are_passed_back_byte_identical() -> None:
    script = three_turns()
    fake = FakeProvider(script)
    agent(fake).predict(view())
    sent = fake.requests[2]["messages"]
    assert canonical_json(sent[1]["content"]) == canonical_json(script[0]["content"])
    assert canonical_json(sent[3]["content"]) == canonical_json(script[1]["content"])
    assert sent[1]["content"][0] == thinking()


def test_request_body_is_pinned() -> None:
    fake = FakeProvider(three_turns())
    agent(fake, effort="medium").predict(view())
    system, task = load_prompt()
    for request in fake.requests:
        assert set(request) == REQUEST_KEYS
        assert request["model"] == MODEL and request["max_tokens"] == MAX_TOKENS == 16000
        assert request["output_config"] == {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": DIAGNOSIS_JSON_SCHEMA},
        }
        assert request["cache_control"] == {"type": "ephemeral"}
        assert request["system"] == system
        assert request["tools"] == api_tools()
        assert request["messages"][0] == {"role": "user", "content": task}
    # System and tools are byte-stable across turns and cases (the cache prefix).
    stable = {canonical_json([r["tools"], r["system"]]) for r in fake.requests}
    assert len(stable) == 1


def test_usage_is_summed_and_costed() -> None:
    fake = FakeProvider(three_turns(), latency=0.1234)
    budget = Budget(None)
    pred = agent(fake, budget).predict(view())
    # sonnet: 2300 in x $2 + 600 out x $10 + 3000 write x $2.50 + 8500 read x $0.10, per M:
    # 4600 + 6000 + 7500 + 850 = 18950 micro-dollars.
    assert pred.usage == {
        "input_tokens": 2300,
        "output_tokens": 600,
        "cache_creation_input_tokens": 3000,
        "cache_read_input_tokens": 8500,
        "api_calls": 3,
        "cost_usd": 0.01895,
        "latency_s": 0.37,
    }
    assert budget.spent == pytest.approx(0.01895, rel=1e-12)


def _run(script: list[dict[str, Any]]) -> Any:
    return agent(FakeProvider(script)).predict(view())


@pytest.mark.parametrize(
    ("last", "outcome", "detail"),
    [
        (
            response("refusal", [], stop_details={"type": "refusal", "category": "cyber"}),
            "refusal",
            '{"category":"cyber","type":"refusal"}',
        ),
        (response("refusal", []), "refusal", None),
        (response("max_tokens", [{"type": "text", "text": "{"}]), "invalid_output", "max_tokens"),
        (response("pause_turn", []), "error", "stop_reason pause_turn"),
        (response(None, []), "error", "stop_reason None"),
        (response("end_turn", [thinking()]), "invalid_output", "no text block"),
    ],
)
def test_stop_reasons_map_to_outcomes(last: dict[str, Any], outcome: str, detail: str) -> None:
    pred = _run([tool_turn(tool_use("t1", "list_units", {})), last])
    assert (pred.outcome, pred.diagnosis, pred.detail) == (outcome, None, detail)
    assert [c["call_id"] for c in pred.trace] == ["c1"] and pred.usage["api_calls"] == 2


def test_invalid_json_is_invalid_output() -> None:
    pred = _run([final(text="not json {")])
    assert pred.outcome == "invalid_output" and pred.diagnosis is None
    assert pred.detail is not None and pred.detail.startswith("Expecting value")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"root_cause_category": "kernel_panic"}, "is not a known category"),
        ({"confidence": 1.5}, "confidence must be a finite number in [0, 1]"),
    ],
)
def test_schema_invalid_diagnosis_is_invalid_output(change: dict[str, Any], message: str) -> None:
    pred = _run([final({**DIAGNOSIS, **change})])
    assert pred.outcome == "invalid_output" and pred.detail is not None
    assert message in pred.detail and len(pred.detail) <= 200


def test_long_error_detail_is_clipped_to_200() -> None:
    pred = _run([final({**DIAGNOSIS, "root_cause_category": "x" * 500})])
    assert pred.outcome == "invalid_output" and pred.detail is not None
    assert len(pred.detail) == 200


def test_the_last_text_block_is_the_diagnosis() -> None:
    content = [
        {"type": "text", "text": "thinking aloud"},
        {"type": "text", "text": json.dumps(DIAGNOSIS)},
    ]
    pred = _run([response("end_turn", content)])
    assert pred.outcome == "ok"


def test_turn_limit() -> None:
    script = [tool_turn(tool_use(f"t{i}", "disk_usage", {})) for i in range(MAX_TURNS)]
    fake = FakeProvider(script)
    pred = agent(fake).predict(view())
    assert (pred.outcome, pred.detail) == ("invalid_output", "turn_limit")
    assert len(fake.requests) == MAX_TURNS == 25 and pred.usage["api_calls"] == 25
    assert len(pred.trace) == 25


def test_unknown_tool_and_non_object_input_become_error_results() -> None:
    fake = FakeProvider(
        [
            tool_turn(
                tool_use("t1", "rm_rf", {"path": "/"}),
                tool_use("t2", "list_units", "not an object"),
                tool_use("t3", None, None),
            ),
            final(),
        ]
    )
    pred = agent(fake).predict(view())
    assert pred.outcome == "ok"
    results = fake.requests[1]["messages"][2]["content"]
    bodies = [json.loads(r["content"]) for r in results]
    assert [b["call_id"] for b in bodies] == ["c1", "c2", "c3"]
    assert [b["result"]["error"]["kind"] for b in bodies] == [
        "unknown_tool",
        "invalid_argument",
        "unknown_tool",
    ]
    assert all(r["is_error"] is False for r in results)
    assert [c["tool"] for c in pred.trace] == ["rm_rf", "list_units", None]
    assert pred.trace[1]["args"] == "not an object"


def test_out_of_range_argument_is_an_error_result_the_model_reads() -> None:
    fake = FakeProvider([tool_turn(tool_use("t1", "list_units", {"limit": 10_000})), final()])
    pred = agent(fake).predict(view())
    body = json.loads(fake.requests[1]["messages"][2]["content"][0]["content"])
    assert body["result"]["error"]["kind"] == "invalid_argument"
    assert pred.outcome == "ok"


def test_tool_use_without_blocks_is_an_error() -> None:
    pred = _run([response("tool_use", [{"type": "text", "text": "hmm"}])])
    assert (pred.outcome, pred.detail) == (
        "error",
        "stop_reason tool_use without a tool_use block",
    )


def test_budget_is_checked_before_the_call() -> None:
    budget = Budget(0.5)
    budget.add(0.5)
    fake = FakeProvider([final()])
    with pytest.raises(AbortRun) as exc:
        agent(fake, budget).predict(view())
    assert fake.requests == []  # no call was made
    assert exc.value.kind == "budget"
    pred = exc.value.prediction
    assert pred.outcome == "budget_exceeded" and pred.trace == []
    assert pred.usage["api_calls"] == 0 and pred.usage["cost_usd"] == 0.0


def test_budget_hit_mid_case_keeps_the_partial_trace() -> None:
    budget = Budget(0.000001)
    fake = FakeProvider([tool_turn(tool_use("t1", "disk_usage", {})), final()])
    with pytest.raises(AbortRun) as exc:
        agent(fake, budget).predict(view())
    assert len(fake.requests) == 1  # overshoot of at most one call
    pred = exc.value.prediction
    assert pred.outcome == "budget_exceeded" and [c["call_id"] for c in pred.trace] == ["c1"]
    assert pred.usage["api_calls"] == 1 and pred.detail is not None


def test_provider_error_aborts_with_the_partial_case() -> None:
    class Broken:
        name = "fake"

        def complete(self, request: dict[str, Any]) -> dict[str, Any]:
            raise ProviderError("BadRequestError (HTTP 400): schema rejected", False)

    with pytest.raises(AbortRun) as exc:
        agent(Broken()).predict(view())
    assert exc.value.kind == "provider"
    pred = exc.value.prediction
    assert (pred.outcome, pred.detail) == ("error", "BadRequestError (HTTP 400): schema rejected")


def test_unknown_model_is_refused_before_any_call() -> None:
    with pytest.raises(UnknownPrice, match="no price for claude-nope"):
        AgentPredictor(FakeProvider([]), "claude-nope", "high", Budget(1))
    with pytest.raises(UnknownPrice, match="no price for claude-nope"):
        DumpPredictor(FakeProvider([]), "claude-nope", "high", Budget(1))


def test_predictor_metadata() -> None:
    a = agent(FakeProvider([]))
    assert (a.name, a.model, a.effort, a.prompt_version) == ("agent", MODEL, "high", "agent-v1")
    assert PROMPT_VERSION == "agent-v1"
    d = DumpPredictor(FakeProvider([]), MODEL, "low", Budget(None))
    assert (d.name, d.model, d.effort, d.prompt_version) == ("dump", MODEL, "low", "dump-v1")


# -- dump predictor ----------------------------------------------------------------------


def test_dump_predictor_sends_one_request_without_tools() -> None:
    fake = FakeProvider([final()])
    pred = DumpPredictor(fake, MODEL, "high", Budget(None)).predict(view())
    trace, text = build_dump(view())
    assert pred.outcome == "ok" and pred.trace == trace
    (request,) = fake.requests
    assert set(request) == REQUEST_KEYS - {"tools"}
    assert request["system"] == prompt_text()
    assert request["messages"] == [{"role": "user", "content": text}]
    assert request["output_config"]["effort"] == "high"
    assert pred.usage["api_calls"] == 1


@pytest.mark.parametrize(
    ("last", "outcome", "detail"),
    [
        (response("refusal", []), "refusal", None),
        (response("max_tokens", []), "invalid_output", "max_tokens"),
        (tool_turn(tool_use("t", "disk_usage", {})), "error", "stop_reason tool_use"),
        (final(text="[]"), "invalid_output", "diagnosis must be an object"),
    ],
)
def test_dump_predictor_outcomes(last: dict[str, Any], outcome: str, detail: str) -> None:
    pred = DumpPredictor(FakeProvider([last]), MODEL, "high", Budget(None)).predict(view())
    assert (pred.outcome, pred.detail, pred.diagnosis) == (outcome, detail, None)


def test_dump_predictor_budget_abort() -> None:
    budget = Budget(1)
    budget.add(1)
    with pytest.raises(AbortRun) as exc:
        DumpPredictor(FakeProvider([]), MODEL, "high", budget).predict(view())
    assert exc.value.kind == "budget" and exc.value.prediction.outcome == "budget_exceeded"
    assert exc.value.prediction.trace  # the dump's calls are made before the model call


def test_case_usage_rounds_cost_to_6_and_latency_to_3_places() -> None:
    # haiku: 2 x (1000 in x $0.10 + 559 out x $0.50) = 759 micro-dollars; latency 2 x 0.12345
    turn = tool_turn(tool_use("t1", "disk_usage", {}), usage=usage(1000, 559))
    fake = FakeProvider([turn, final(usage=usage(1000, 559))], latency=0.12345)
    pred = AgentPredictor(fake, "claude-haiku-5-5", "medium", Budget(None)).predict(view())
    assert pred.usage["cost_usd"] == 0.000759 and pred.usage["latency_s"] == 0.247
    assert pred.exact_cost_usd == pytest.approx(0.000759, rel=1e-12)
