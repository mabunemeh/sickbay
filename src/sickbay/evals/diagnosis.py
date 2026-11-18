"""Diagnosis schema 1: what a predictor answers, and its client-side validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Bump on any change to CATEGORIES or to the diagnosis shape.
DIAGNOSIS_SCHEMA_VERSION = 1

# Frozen root-cause categories (shared with label schema 1).
CATEGORIES: tuple[str, ...] = (
    "fd_limit",
    "memory_limit",
    "crash_loop",
    "disk_full",
    "dns",
    "tls_cert",
    "port_conflict",
    "config_error",
    "nothing_wrong",
)

NOTHING_WRONG = "nothing_wrong"

_KEYS = frozenset({"root_cause_category", "component", "evidence", "fix", "confidence"})
_EVIDENCE_KEYS = frozenset({"call_id", "quote"})

# Passed as output_config.format by the model predictors (batch 4). Only the keywords
# structured outputs support: type, properties, items, enum, required and
# additionalProperties (false on every object). Bounds are checked by validate_diagnosis.
DIAGNOSIS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "root_cause_category": {"type": "string", "enum": list(CATEGORIES)},
        "component": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "call_id": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["call_id", "quote"],
                "additionalProperties": False,
            },
        },
        "fix": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["root_cause_category", "component", "evidence", "fix", "confidence"],
    "additionalProperties": False,
}


class DiagnosisError(ValueError):
    """The predictor's output is not a valid schema 1 diagnosis."""


@dataclass(frozen=True)
class EvidenceRef:
    call_id: str
    quote: str


@dataclass(frozen=True)
class Diagnosis:
    root_cause_category: str
    component: str
    evidence: tuple[EvidenceRef, ...]
    fix: str
    confidence: float

    def to_json(self) -> dict[str, Any]:
        return {
            "root_cause_category": self.root_cause_category,
            "component": self.component,
            "evidence": [{"call_id": e.call_id, "quote": e.quote} for e in self.evidence],
            "fix": self.fix,
            "confidence": self.confidence,
        }


def _string(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise DiagnosisError(f"{where} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise DiagnosisError(f"{where} is not valid UTF-8") from None
    if "\x00" in value:
        raise DiagnosisError(f"{where} contains NUL")
    return value


def _keys(obj: object, expected: frozenset[str], where: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise DiagnosisError(f"{where} must be an object")
    keys = set(obj)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(repr(k) for k in keys - expected)
        raise DiagnosisError(f"{where} keys: missing {missing}, unexpected {extra}")
    return obj


def validate_diagnosis(obj: object) -> Diagnosis:
    """Check a decoded diagnosis object completely; raise DiagnosisError on any problem."""
    data = _keys(obj, _KEYS, "diagnosis")
    category = _string(data["root_cause_category"], "root_cause_category")
    if category not in CATEGORIES:
        raise DiagnosisError(f"root_cause_category {category!r} is not a known category")
    component = _string(data["component"], "component")
    if not component.strip() and category != NOTHING_WRONG:
        raise DiagnosisError("component may be empty only with nothing_wrong")
    raw_evidence = data["evidence"]
    if not isinstance(raw_evidence, list):
        raise DiagnosisError("evidence must be an array")
    evidence = []
    for i, item in enumerate(raw_evidence):
        fields = _keys(item, _EVIDENCE_KEYS, f"evidence[{i}]")
        evidence.append(
            EvidenceRef(
                call_id=_string(fields["call_id"], f"evidence[{i}].call_id"),
                quote=_string(fields["quote"], f"evidence[{i}].quote"),
            )
        )
    fix = _string(data["fix"], "fix")
    confidence = data["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        raise DiagnosisError("confidence must be a number")
    if not 0 <= confidence <= 1:  # also rejects NaN and inf; no float() of a huge int first
        raise DiagnosisError("confidence must be a finite number in [0, 1]")
    return Diagnosis(category, component, tuple(evidence), fix, float(confidence))
