"""Runs a predictor over the incidents and builds the scored result JSON (schema 1)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sickbay import __version__
from sickbay.evals.diagnosis import DIAGNOSIS_SCHEMA_VERSION
from sickbay.evals.labels import LABEL_SCHEMA_VERSION, Label, load_label
from sickbay.evals.predictor import OUTCOMES, Prediction, Predictor
from sickbay.evals.scorers import aggregate_bool, aggregate_mean, score_case
from sickbay.snapshot import Snapshot, SnapshotError
from sickbay.tools import TOOLSET_VERSION

RESULT_SCHEMA_VERSION = 1

__all__ = [
    "RESULT_SCHEMA_VERSION",
    "Prediction",
    "Predictor",
    "dev_incidents",
    "dumps_result",
    "incident_dirs",
    "open_incident",
    "run_eval",
    "summary_line",
]


def incident_dirs(incidents_dir: Path) -> list[Path]:
    """Every incident directory, in sorted id order. SnapshotError if there are none."""
    try:
        dirs = sorted((p for p in incidents_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError as exc:
        raise SnapshotError(f"cannot list incidents in {incidents_dir}: {exc}") from exc
    if not dirs:
        raise SnapshotError(f"no incidents in {incidents_dir}")
    return dirs


def open_incident(incident_dir: Path) -> Snapshot:
    """Open and verify incident_dir/snapshot; SnapshotError on any problem."""
    snap = Snapshot.open(incident_dir / "snapshot")
    problems = snap.verify()
    if problems:
        raise SnapshotError(f"{incident_dir.name}: snapshot fails verification: {problems[0]}")
    return snap


def dev_incidents(
    incidents_dir: Path, note: Callable[[str], None] | None = None
) -> list[tuple[Path, Label]]:
    """(directory, label) of every non-heldout incident, sorted by id.

    Every label is loaded (LabelError if any is bad); heldout ones are skipped with a
    message to `note`.
    """
    found = []
    for incident_dir in incident_dirs(incidents_dir):
        label = load_label(incident_dir / "label.yaml")
        if label.split == "heldout":
            if note is not None:
                note(f"skipped heldout incident {label.id}")
            continue
        found.append((incident_dir, label))
    return found


def _case(label: Label, repeat: int, prediction: Prediction) -> dict[str, Any]:
    diagnosis = prediction.diagnosis if prediction.outcome == "ok" else None
    return {
        "incident": label.id,
        "repeat": repeat,
        "outcome": prediction.outcome,
        "diagnosis": diagnosis.to_json() if diagnosis is not None else None,
        "trace": prediction.trace,
        "scores": score_case(label, diagnosis, prediction.trace),
        "usage": prediction.usage,
    }


def _summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [c["scores"] for c in cases]
    return {
        "cases": len(cases),
        "outcomes": {o: sum(1 for c in cases if c["outcome"] == o) for o in OUTCOMES},
        "category": aggregate_bool([s["category"] for s in scores]),
        "component": aggregate_bool([s["component"] for s in scores]),
        "grounding_mean": aggregate_mean([s["grounding"] for s in scores]),
        "fix": None,
        "safety": None,
    }


def run_eval(
    predictor: Predictor,
    incidents_dir: Path,
    repeats: int = 1,
    note: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run predictor on every dev incident, `repeats` times each, and score it.

    Raises LabelError or SnapshotError when an incident's label or snapshot is unreadable
    or invalid. Heldout incidents are skipped, with a message to `note`.
    """
    cases = []
    for incident_dir, label in dev_incidents(incidents_dir, note):
        snap = open_incident(incident_dir)
        for repeat in range(repeats):
            cases.append(_case(label, repeat, predictor.predict(snap)))
    return {
        "result_schema": RESULT_SCHEMA_VERSION,
        "versions": {
            "sickbay": __version__,
            "toolset": TOOLSET_VERSION,
            "label_schema": LABEL_SCHEMA_VERSION,
            "diagnosis_schema": DIAGNOSIS_SCHEMA_VERSION,
        },
        "run": {
            "predictor": predictor.name,
            "model": predictor.model,
            "effort": predictor.effort,
            "prompt_version": predictor.prompt_version,
            "repeats": repeats,
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "cases": cases,
        "summary": _summary(cases),
    }


def dumps_result(result: dict[str, Any]) -> bytes:
    """Sorted keys, indent 2, UTF-8, LF, trailing newline."""
    text = json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
    return (text + "\n").encode("utf-8")


def summary_line(result: dict[str, Any]) -> str:
    """e.g. `keyword: category 2/3, component 3/3, grounding 1.000 (n=3)`."""
    summary = result["summary"]
    cat, comp, ground = summary["category"], summary["component"], summary["grounding_mean"]
    value = "n/a" if ground["value"] is None else f"{ground['value']:.3f}"
    return (
        f"{result['run']['predictor']}: category {cat['correct']}/{cat['n']}, "
        f"component {comp['correct']}/{comp['n']}, grounding {value} (n={ground['n']})"
    )
