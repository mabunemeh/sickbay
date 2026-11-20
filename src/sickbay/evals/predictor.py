"""The Predictor protocol every baseline and agent implements, and its Prediction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sickbay.evals.diagnosis import Diagnosis
from sickbay.evals.trace import TraceEntry
from sickbay.snapshot.hostview import HostView

# The non-model baselines produce only ok and invalid_output; the rest come from models.
Outcome = Literal["ok", "invalid_output", "refusal", "error", "budget_exceeded"]
OUTCOMES: tuple[Outcome, ...] = ("ok", "invalid_output", "refusal", "error", "budget_exceeded")


def empty_usage() -> dict[str, Any]:
    """Usage of a predictor that calls no model."""
    return {
        "input_tokens": None,
        "output_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
        "api_calls": 0,
        "cost_usd": 0.0,
        "latency_s": None,
    }


@dataclass(frozen=True)
class Prediction:
    outcome: Outcome
    diagnosis: Diagnosis | None  # None unless outcome is ok
    trace: list[TraceEntry]
    usage: dict[str, Any]
    detail: str | None = None  # why the case ended this way (error text, stop details)
    # The case's cost before rounding; the run totals sum these (not serialised).
    exact_cost_usd: float = 0.0


AbortKind = Literal["budget", "provider"]


class AbortRun(Exception):
    """Raised by predict(): record `prediction` for this case, then stop the run.

    kind "budget": the remaining cases are recorded as budget_exceeded (exit 5);
    kind "provider": the remaining cases are not run (exit 4).
    """

    def __init__(self, prediction: Prediction, kind: AbortKind) -> None:
        super().__init__(prediction.detail or kind)
        self.prediction = prediction
        self.kind = kind


class Predictor(Protocol):
    name: str
    model: str | None
    effort: str | None
    prompt_version: str | None

    def predict(self, view: HostView) -> Prediction: ...
