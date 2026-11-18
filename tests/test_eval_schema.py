"""Diagnosis schema 1 and label schema 1."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
import yaml
from evalfakes import label_data, label_yaml

from sickbay.evals.diagnosis import (
    CATEGORIES,
    DIAGNOSIS_JSON_SCHEMA,
    DIAGNOSIS_SCHEMA_VERSION,
    DiagnosisError,
    validate_diagnosis,
)
from sickbay.evals.labels import LABEL_SCHEMA_VERSION, LabelError, load_label

ALLOWED_KEYWORDS = {"type", "properties", "items", "enum", "required", "additionalProperties"}
ALLOWED_TYPES = {"object", "array", "string", "number"}


def good() -> dict[str, Any]:
    return {
        "root_cause_category": "fd_limit",
        "component": "app.service",
        "evidence": [{"call_id": "c1", "quote": "Too many open files"}],
        "fix": "Raise the limit.",
        "confidence": 0.5,
    }


def test_versions_and_frozen_categories() -> None:
    assert DIAGNOSIS_SCHEMA_VERSION == 1
    assert LABEL_SCHEMA_VERSION == 1
    assert CATEGORIES == (
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


def _walk_schema(node: Any, where: str) -> None:
    assert isinstance(node, dict), where
    extra = set(node) - ALLOWED_KEYWORDS
    assert not extra, f"{where}: keywords not supported by structured outputs: {extra}"
    assert node.get("type") in ALLOWED_TYPES, where
    if node["type"] == "object":
        assert node.get("additionalProperties") is False, where
        props = node["properties"]
        assert set(node["required"]) == set(props), where
        for name, sub in props.items():  # names are data, not keywords
            _walk_schema(sub, f"{where}.{name}")
    else:
        assert "properties" not in node and "required" not in node, where
        assert "additionalProperties" not in node, where
    if node["type"] == "array":
        _walk_schema(node["items"], f"{where}[]")
    else:
        assert "items" not in node, where


def test_json_schema_uses_only_supported_keywords() -> None:
    _walk_schema(DIAGNOSIS_JSON_SCHEMA, "$")
    assert DIAGNOSIS_JSON_SCHEMA["properties"]["root_cause_category"]["enum"] == list(CATEGORIES)


def test_json_schema_walker_catches_a_bound() -> None:
    bad: dict[str, Any] = {
        "type": "object",
        "properties": {"x": {"type": "number", "minimum": 0}},
        "required": ["x"],
        "additionalProperties": False,
    }
    with pytest.raises(AssertionError, match="minimum"):
        _walk_schema(bad, "$")


def test_valid_diagnosis() -> None:
    d = validate_diagnosis(good())
    assert d.root_cause_category == "fd_limit"
    assert d.evidence[0].call_id == "c1"
    assert d.to_json() == good()
    assert validate_diagnosis({**good(), "confidence": 1}).confidence == 1.0
    nothing = {**good(), "root_cause_category": "nothing_wrong", "component": "", "evidence": []}
    assert validate_diagnosis(nothing).component == ""


@pytest.mark.parametrize(
    "confidence", [True, False, 1.5, -0.1, math.nan, math.inf, -math.inf, 10**400, "0.5", None]
)
def test_bad_confidence_rejected(confidence: object) -> None:
    with pytest.raises(DiagnosisError, match="confidence"):
        validate_diagnosis({**good(), "confidence": confidence})


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"root_cause_category": "oom"},
        {"root_cause_category": 3},
        {"component": ""},
        {"component": "  "},
        {"component": "a\x00b"},
        {"fix": "\ud800"},
        {"evidence": {"call_id": "c1", "quote": "x"}},
        {"evidence": [{"call_id": "c1"}]},
        {"evidence": [{"call_id": "c1", "quote": "x", "more": 1}]},
        {"evidence": [{"call_id": 1, "quote": "abcdefgh"}]},
        {"evidence": ["c1"]},
        {"evidence": {}},
        {"evidence": None},
    ],
)
def test_bad_diagnosis_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(DiagnosisError):
        validate_diagnosis({**good(), **change})


def test_missing_key_and_non_object_rejected() -> None:
    obj = good()
    del obj["fix"]
    with pytest.raises(DiagnosisError, match="missing"):
        validate_diagnosis(obj)
    with pytest.raises(DiagnosisError):
        validate_diagnosis([good()])


# -- labels ---------------------------------------------------------------------------------


def write_label(tmp_path: Path, text: str, incident: str = "case-a") -> Path:
    path = tmp_path / incident / "label.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def test_label_loads(tmp_path: Path) -> None:
    label = load_label(write_label(tmp_path, label_yaml(also_accepted=["other.service"])))
    assert label.id == "case-a" and label.label_schema == 1 and label.split == "dev"
    assert label.also_accepted == ("other.service",)
    assert label.evidence[0].source == "journal"
    nothing = label_yaml(root_cause_category="nothing_wrong", component=None)
    assert load_label(write_label(tmp_path, nothing)).component is None


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"surprise": 1}, "surprise"),
        ({"label_schema": 0}, "label_schema"),
        ({"label_schema": True}, "label_schema"),
        ({"label_schema": 1.0}, "label_schema"),
        ({"root_cause_category": "oom"}, "root_cause_category"),
        ({"component": None}, "component"),
        ({"component": ""}, "component"),
        ({"status": "draft - awaiting review"}, "status"),
        ({"split": "test"}, "split"),
        ({"id": "case-b"}, "id"),
        ({"injection": "no"}, "injection"),
        ({"also_accepted": "x.service"}, "also_accepted"),
        ({"evidence": []}, "evidence"),
        ({"evidence": [{"source": "journal"}]}, "evidence[0]"),
        ({"evidence": [{"source": "journal", "contains": "x", "more": 1}]}, "evidence[0]"),
        ({"title": ""}, "title"),
        ({"names": [1]}, "names[0]"),
    ],
)
def test_bad_label_is_a_label_error(tmp_path: Path, changes: dict[str, Any], field: str) -> None:
    path = write_label(tmp_path, label_yaml(**changes))
    with pytest.raises(LabelError) as exc:
        load_label(path)
    assert str(path) in str(exc.value) and f": {field}:" in str(exc.value)


def test_missing_field_is_a_label_error(tmp_path: Path) -> None:
    data = label_data()
    del data["split"]
    path = write_label(tmp_path, yaml.safe_dump(data))
    with pytest.raises(LabelError, match="split: missing"):
        load_label(path)


def test_unreadable_label_is_a_label_error(tmp_path: Path) -> None:
    with pytest.raises(LabelError):
        load_label(tmp_path / "nope" / "label.yaml")
    with pytest.raises(LabelError):
        load_label(write_label(tmp_path, "a: [unclosed"))
    with pytest.raises(LabelError):
        bad_date = label_yaml().replace("title: A title", "title: 2026-13-45")
        load_label(write_label(tmp_path, bad_date))
    with pytest.raises(LabelError, match="mapping"):
        load_label(write_label(tmp_path, "- a list\n"))


def test_integer_confidence_comes_back_as_float() -> None:
    confidence = validate_diagnosis({**good(), "confidence": 1}).confidence
    assert confidence == 1.0 and type(confidence) is float


def test_duplicate_label_key_is_a_label_error(tmp_path: Path) -> None:
    text = label_yaml().replace("status: draft\n", "status: draft\nstatus: reviewed\n")
    assert text.count("status:") == 2
    with pytest.raises(LabelError, match="duplicate key 'status'"):
        load_label(write_label(tmp_path, text))


def test_duplicate_key_in_a_nested_mapping_is_a_label_error(tmp_path: Path) -> None:
    text = label_yaml().replace("source: journal\n", "source: journal\n  source: journal\n")
    assert text.count("source:") == 2
    with pytest.raises(LabelError, match="duplicate key"):
        load_label(write_label(tmp_path, text))
