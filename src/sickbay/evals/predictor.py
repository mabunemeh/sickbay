"""The Predictor protocol every baseline and agent implements, and its Prediction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sickbay.evals.diagnosis import Diagnosis
from sickbay.evals.trace import TraceEntry
from sickbay.snapshot.hostview import HostView

# Batch 3 produces only ok and invalid_output; the rest are for model predictors.
Outcome = Literal["ok", "invalid_output", "refusal", "error", "budget_exceeded"]
OUTCOMES: tuple[Outcome, ...] = ("ok", "invalid_output", "refusal", "error", "budget_exceeded")


def empty_usage() -> dict[str, Any]:
    """Usage of a predictor that calls no model."""
    return {"input_tokens": None, "output_tokens": None, "cost_usd": 0.0, "latency_s": None}


@dataclass(frozen=True)
class Prediction:
    outcome: Outcome
    diagnosis: Diagnosis | None  # None unless outcome is ok
    trace: list[TraceEntry]
    usage: dict[str, Any]


class Predictor(Protocol):
    name: str
    model: str | None
    effort: str | None
    prompt_version: str | None

    def predict(self, view: HostView) -> Prediction: ...
