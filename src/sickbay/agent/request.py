"""What the agent and the dump predictor share: the request body, the metered call, and
the mapping of a final response to an outcome.

Every request pins effort explicitly and carries the diagnosis schema as structured output.
It never sends thinking, temperature/top_p/top_k, tool_choice, fallbacks or betas.
"""

from __future__ import annotations

import json
from typing import Any

from sickbay.agent.pricing import Budget, BudgetExceeded, call_cost
from sickbay.agent.providers.base import LATENCY_KEY, Provider, ProviderError, canonical_json
from sickbay.evals.diagnosis import DIAGNOSIS_JSON_SCHEMA, Diagnosis, validate_diagnosis
from sickbay.evals.predictor import AbortRun, Outcome, Prediction
from sickbay.evals.trace import TraceEntry

MAX_TOKENS = 16_000
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Used when --effort is omitted; always sent explicitly and recorded in run.effort.
DEFAULT_EFFORT: dict[str, str] = {
    "claude-haiku-5-5": "medium",
    "claude-sonnet-5-5": "high",
    "claude-opus-5-5": "medium",
    "claude-fable-5-2": "high",
}
DETAIL_CHARS = 200
_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def build_request(
    model: str,
    effort: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The exact Messages API body. messages is referenced, not copied: hash it at once."""
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": messages,
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": DIAGNOSIS_JSON_SCHEMA},
        },
        "cache_control": {"type": "ephemeral"},
    }
    if tools is not None:
        body["tools"] = tools
    return body


def model_usage() -> dict[str, Any]:
    """Usage of a model case that made no call (all counts zero)."""
    usage: dict[str, Any] = dict.fromkeys(_TOKEN_KEYS, 0)
    usage.update({"api_calls": 0, "cost_usd": 0.0, "latency_s": 0.0})
    return usage


class Meter:
    """Checks the budget before each call, and sums tokens, cost and latency after it."""

    def __init__(self, provider: Provider, model: str, budget: Budget) -> None:
        self.provider = provider
        self.model = model
        self.budget = budget
        self.tokens = dict.fromkeys(_TOKEN_KEYS, 0)
        self.api_calls = 0
        self.cost = 0.0
        self.latency = 0.0

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        """budget.check() (BudgetExceeded), then the call; ProviderError propagates."""
        self.budget.check()
        response = self.provider.complete(request)
        latency = response.pop(LATENCY_KEY, 0.0)
        usage = response.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        cost = call_cost(self.model, usage)
        self.budget.add(cost)
        self.api_calls += 1
        self.cost += cost
        self.latency += float(latency)
        for key in _TOKEN_KEYS:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                self.tokens[key] += value
        return response

    def prediction(
        self,
        outcome: Outcome,
        diagnosis: Diagnosis | None,
        trace: list[TraceEntry],
        detail: str | None,
    ) -> Prediction:
        return Prediction(outcome, diagnosis, trace, self.usage(), detail, self.cost)

    def usage(self) -> dict[str, Any]:
        return {
            **self.tokens,
            "api_calls": self.api_calls,
            "cost_usd": round(self.cost, 6),
            "latency_s": round(self.latency, 3),
        }


def _clip(text: str) -> str:
    return text[:DETAIL_CHARS]


def final_outcome(response: dict[str, Any]) -> tuple[Outcome, Diagnosis | None, str | None]:
    """(outcome, diagnosis, detail) for a response that ends the case (not tool_use)."""
    stop = response.get("stop_reason")
    if stop == "refusal":
        details = response.get("stop_details")
        return "refusal", None, None if details is None else canonical_json(details)
    if stop == "max_tokens":
        return "invalid_output", None, "max_tokens"
    if stop != "end_turn":
        return "error", None, f"stop_reason {stop}"
    content = response.get("content")
    texts = [
        b["text"]
        for b in (content if isinstance(content, list) else [])
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]
    if not texts:
        return "invalid_output", None, "no text block"
    try:
        diagnosis = validate_diagnosis(json.loads(texts[-1]))
    except ValueError as exc:  # JSONDecodeError and DiagnosisError are ValueErrors
        return "invalid_output", None, _clip(str(exc))
    return "ok", diagnosis, None


def call_or_abort(meter: Meter, request: dict[str, Any], trace: list[TraceEntry]) -> dict[str, Any]:
    """meter.call(request); a spent budget or a provider error becomes AbortRun carrying
    this case's partial prediction (trace so far, usage so far)."""
    try:
        return meter.call(request)
    except BudgetExceeded as exc:
        prediction = meter.prediction("budget_exceeded", None, trace, str(exc))
        raise AbortRun(prediction, "budget") from None
    except ProviderError as exc:
        prediction = meter.prediction("error", None, trace, exc.message)
        raise AbortRun(prediction, "provider") from None
