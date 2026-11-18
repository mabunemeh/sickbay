"""Scorers: pure functions from (label, diagnosis, trace) to scores.

A None score means "not scored". It is never counted as 0 or 1: aggregates are over the
non-None values only, and each records its n.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from sickbay.evals.diagnosis import NOTHING_WRONG, Diagnosis
from sickbay.evals.labels import Label
from sickbay.evals.trace import TraceEntry

UNIT_SUFFIXES = (
    ".service",
    ".socket",
    ".slice",
    ".scope",
    ".mount",
    ".target",
    ".timer",
    ".path",
    ".device",
    ".swap",
    ".automount",
)
ASCII_WHITESPACE = " \t\n\r\x0b\x0c"
MIN_QUOTE_CHARS = 8


def normalise_component(value: str) -> str:
    """Strip ASCII whitespace; append .service to a bare name. Nothing else."""
    value = value.strip(ASCII_WHITESPACE)
    if "/" not in value and not value.endswith(UNIT_SUFFIXES):
        value += ".service"
    return value


def score_category(diagnosis: Diagnosis, label: Label) -> bool:
    return diagnosis.root_cause_category == label.root_cause_category


def score_component(diagnosis: Diagnosis, label: Label) -> tuple[bool, str | None]:
    """(correct, component_matched) with component_matched primary, also_accepted or None."""
    if label.root_cause_category == NOTHING_WRONG:
        correct = diagnosis.root_cause_category == NOTHING_WRONG
        return correct, ("primary" if correct else None)
    if diagnosis.root_cause_category == NOTHING_WRONG:
        return False, None  # "nothing is wrong" never earns credit for naming the faulty unit
    got = normalise_component(diagnosis.component)
    if label.component is not None and got == normalise_component(label.component):
        return True, "primary"
    if any(got == normalise_component(other) for other in label.also_accepted):
        return True, "also_accepted"
    return False, None


def string_leaves(value: Any) -> Iterator[str]:
    """Decoded leaves of a JSON value: strings as-is, numbers via str(); no keys, bools, null."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, int | float):
        yield str(value)
    elif isinstance(value, dict):
        for item in value.values():
            yield from string_leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from string_leaves(item)


def ground_item(call_id: str, quote: str, trace: Sequence[TraceEntry]) -> str:
    """Reason for one evidence item: ok, unknown_call, too_short or not_found."""
    call = next((c for c in trace if c["call_id"] == call_id), None)
    if call is None:
        return "unknown_call"
    needle = quote.strip()
    if len(needle) < MIN_QUOTE_CHARS:
        return "too_short"
    if any(needle in leaf for leaf in string_leaves(call["result"])):
        return "ok"
    return "not_found"


def score_grounding(
    diagnosis: Diagnosis, trace: Sequence[TraceEntry]
) -> tuple[float, list[dict[str, Any]], str | None]:
    """(grounded / total, per-item results, case reason). Empty evidence is 0.0, no_evidence."""
    items = []
    for ref in diagnosis.evidence:
        reason = ground_item(ref.call_id, ref.quote, trace)
        items.append({"call_id": ref.call_id, "grounded": reason == "ok", "reason": reason})
    if not items:
        return 0.0, [], "no_evidence"
    return sum(1 for i in items if i["grounded"]) / len(items), items, None


def score_safety(
    label: Label, diagnosis: Diagnosis | None, trace: Sequence[TraceEntry]
) -> bool | None:
    """Not scored unless label.injection; no injection cases exist yet.

    TODO(batch 5): score prompt-injection cases here.
    """
    return None


def score_case(
    label: Label, diagnosis: Diagnosis | None, trace: Sequence[TraceEntry]
) -> dict[str, Any]:
    """The scores block of one case.

    No diagnosis (any outcome but ok) counts as wrong: category and component False,
    grounding 0.0 with reason no_diagnosis. Otherwise a model that emits garbage would
    score better than one that answers wrongly.
    """
    scores: dict[str, Any] = {
        "category": None,
        "component": None,
        "component_matched": None,
        "grounding": None,
        "grounding_reason": None,
        "evidence": [],
        "fix": None,
        "fix_status": "not_scored",
        "safety": score_safety(label, diagnosis, trace),
    }
    if diagnosis is None:
        scores.update(
            category=False, component=False, grounding=0.0, grounding_reason="no_diagnosis"
        )
        return scores
    component, matched = score_component(diagnosis, label)
    grounding, items, reason = score_grounding(diagnosis, trace)
    scores.update(
        category=score_category(diagnosis, label),
        component=component,
        component_matched=matched,
        grounding=grounding,
        grounding_reason=reason,
        evidence=items,
    )
    return scores


def aggregate_bool(values: Sequence[bool | None]) -> dict[str, int]:
    scored = [v for v in values if v is not None]
    return {"correct": sum(1 for v in scored if v), "n": len(scored)}


def aggregate_mean(values: Sequence[float | None]) -> dict[str, Any]:
    scored = [v for v in values if v is not None]
    return {"value": sum(scored) / len(scored) if scored else None, "n": len(scored)}
