"""The runner, the result JSON (golden) and the committed keyword result."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from evalfakes import label_yaml
from make_eval_golden import CREATED_AT, GOLDEN, REPO, keyword_result

from sickbay.evals.baselines.keyword import KeywordPredictor
from sickbay.evals.diagnosis import validate_diagnosis
from sickbay.evals.labels import LabelError
from sickbay.evals.predictor import Prediction, empty_usage
from sickbay.evals.runner import (
    RESULT_SCHEMA_VERSION,
    dev_incidents,
    dumps_result,
    run_eval,
    summary_line,
)
from sickbay.snapshot import SnapshotError
from sickbay.snapshot.hostview import HostView
from sickbay.tools import TOOLSET_VERSION

BASIC = Path(__file__).parent / "fixtures" / "basic"
COMMITTED = REPO / "results" / "keyword-baseline.json"
_CREATED = re.compile(rb'"created_at": "[^"]*"')


def make_incidents(root: Path, labels: dict[str, str]) -> Path:
    for incident, text in labels.items():
        shutil.copytree(BASIC, root / incident / "snapshot")
        (root / incident / "label.yaml").write_bytes(text.encode("utf-8"))
    return root


def test_versions() -> None:
    assert RESULT_SCHEMA_VERSION == 1
    assert TOOLSET_VERSION == 1


def test_keyword_golden_result() -> None:
    """Byte-exact except created_at. Regenerate with python tests/make_eval_golden.py."""
    assert dumps_result(keyword_result()) == GOLDEN.read_bytes()


def test_committed_result_matches_golden() -> None:
    data = COMMITTED.read_bytes()
    created = json.loads(data)["run"]["created_at"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", created)
    assert _CREATED.sub(f'"created_at": "{CREATED_AT}"'.encode(), data) == GOLDEN.read_bytes()


def test_result_shape_and_serialisation(tmp_path: Path) -> None:
    root = make_incidents(
        tmp_path, {"b-case": label_yaml("b-case"), "a-case": label_yaml("a-case")}
    )
    result = run_eval(KeywordPredictor(), root, repeats=2)
    assert [(c["incident"], c["repeat"]) for c in result["cases"]] == [
        ("a-case", 0),
        ("a-case", 1),
        ("b-case", 0),
        ("b-case", 1),
    ]
    assert result["versions"] == {
        "sickbay": result["versions"]["sickbay"],
        "toolset": 1,
        "label_schema": 1,
        "diagnosis_schema": 1,
    }
    assert result["run"]["predictor"] == "keyword" and result["run"]["repeats"] == 2
    assert result["run"]["model"] is None and result["run"]["prompt_version"] is None
    data = dumps_result(result)
    assert data.endswith(b"}\n") and b"\r" not in data
    assert data == (json.dumps(json.loads(data), sort_keys=True, indent=2) + "\n").encode()


class _Fixed:
    name = "fixed"
    model: str | None = "m"
    effort: str | None = None
    prompt_version: str | None = "p"

    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = outcomes

    def predict(self, view: HostView) -> Prediction:
        if self.outcomes.pop(0) == "ok":
            diagnosis = validate_diagnosis(
                {
                    "root_cause_category": "fd_limit",
                    "component": "app",
                    "evidence": [],
                    "fix": "f",
                    "confidence": 1,
                }
            )
            return Prediction("ok", diagnosis, [], empty_usage())
        return Prediction("invalid_output", None, [], empty_usage())


def test_invalid_output_counts_as_wrong(tmp_path: Path) -> None:
    root = make_incidents(tmp_path, {"a-case": label_yaml("a-case")})
    result = run_eval(_Fixed(["ok", "invalid_output"]), root, repeats=2)
    bad = result["cases"][1]
    assert bad["outcome"] == "invalid_output" and bad["diagnosis"] is None
    assert bad["scores"]["category"] is False and bad["scores"]["component"] is False
    assert bad["scores"]["grounding"] == 0.0
    assert bad["scores"]["grounding_reason"] == "no_diagnosis"
    assert bad["scores"]["evidence"] == [] and bad["scores"]["component_matched"] is None
    assert result["summary"] == {
        "cases": 2,
        "outcomes": {
            "ok": 1,
            "invalid_output": 1,
            "refusal": 0,
            "error": 0,
            "budget_exceeded": 0,
        },
        "category": {"correct": 1, "n": 2},
        "component": {"correct": 1, "n": 2},
        "grounding_mean": {"value": 0.0, "n": 2},
        "fix": None,
        "safety": None,
    }
    assert summary_line(result) == "fixed: category 1/2, component 1/2, grounding 0.000 (n=2)"


def test_only_invalid_output_scores_zero_not_excluded(tmp_path: Path) -> None:
    root = make_incidents(tmp_path, {"a-case": label_yaml("a-case")})
    result = run_eval(_Fixed(["invalid_output"]), root)
    assert result["summary"]["category"] == {"correct": 0, "n": 1}
    assert result["summary"]["component"] == {"correct": 0, "n": 1}
    assert result["summary"]["grounding_mean"] == {"value": 0.0, "n": 1}
    assert result["summary"]["outcomes"]["invalid_output"] == 1


def test_heldout_is_skipped_with_a_note(tmp_path: Path) -> None:
    root = make_incidents(
        tmp_path, {"a-case": label_yaml("a-case"), "h-case": label_yaml("h-case", split="heldout")}
    )
    notes: list[str] = []
    result = run_eval(KeywordPredictor(), root, note=notes.append)
    assert [c["incident"] for c in result["cases"]] == ["a-case"]
    assert notes == ["skipped heldout incident h-case"]


def test_no_scored_cases_summary_line(tmp_path: Path) -> None:
    root = make_incidents(tmp_path, {"h-case": label_yaml("h-case", split="heldout")})
    result = run_eval(KeywordPredictor(), root)
    assert result["summary"]["grounding_mean"] == {"value": None, "n": 0}
    assert summary_line(result).endswith("grounding n/a (n=0)")


def test_bad_inputs_raise(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError):
        run_eval(KeywordPredictor(), tmp_path / "missing")
    with pytest.raises(SnapshotError):
        run_eval(KeywordPredictor(), tmp_path)  # empty
    root = make_incidents(tmp_path / "x", {"a-case": label_yaml("a-case", label_schema=0)})
    with pytest.raises(LabelError):
        run_eval(KeywordPredictor(), root)
    root = make_incidents(tmp_path / "y", {"a-case": label_yaml("a-case")})
    (root / "a-case" / "snapshot" / "commands" / "df.txt").write_bytes(b"tampered")
    with pytest.raises(SnapshotError, match="verification"):
        run_eval(KeywordPredictor(), root)


class _ErrorWithDiagnosis:
    name = "errs"
    model: str | None = "model-x"
    effort: str | None = "high"
    prompt_version: str | None = "prompt-v9"

    def predict(self, view: HostView) -> Prediction:
        diagnosis = validate_diagnosis(
            {
                "root_cause_category": "fd_limit",
                "component": "app.service",
                "evidence": [],
                "fix": "f",
                "confidence": 1,
            }
        )
        return Prediction("error", diagnosis, [], empty_usage())


def test_error_outcome_with_a_diagnosis_still_counts_wrong(tmp_path: Path) -> None:
    root = make_incidents(tmp_path, {"a-case": label_yaml("a-case")})
    result = run_eval(_ErrorWithDiagnosis(), root)
    case = result["cases"][0]
    assert case["outcome"] == "error" and case["diagnosis"] is None
    scores = case["scores"]
    assert (scores["category"], scores["component"], scores["grounding"]) == (False, False, 0.0)
    assert scores["grounding_reason"] == "no_diagnosis"
    assert result["summary"]["outcomes"]["error"] == 1
    assert result["summary"]["category"] == {"correct": 0, "n": 1}


def test_predictor_metadata_is_passed_through(tmp_path: Path) -> None:
    root = make_incidents(tmp_path, {"a-case": label_yaml("a-case")})
    run = run_eval(_ErrorWithDiagnosis(), root, repeats=1)["run"]
    assert (run["predictor"], run["model"], run["effort"], run["prompt_version"]) == (
        "errs",
        "model-x",
        "high",
        "prompt-v9",
    )


def test_non_ascii_is_written_unescaped() -> None:
    data = dumps_result({"m": "caf\u00e9 \u2026"})
    assert data == '{\n  "m": "caf\u00e9 \u2026"\n}\n'.encode()
    assert b"\\u" not in data


def test_dev_incidents_skips_heldout(tmp_path: Path) -> None:
    root = make_incidents(
        tmp_path, {"a-case": label_yaml("a-case"), "h-case": label_yaml("h-case", split="heldout")}
    )
    notes: list[str] = []
    found = dev_incidents(root, notes.append)
    assert [(d.name, label.id) for d, label in found] == [("a-case", "a-case")]
    assert notes == ["skipped heldout incident h-case"]
