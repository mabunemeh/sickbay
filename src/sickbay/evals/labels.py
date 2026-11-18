"""Label schema 1: the ground truth for one incident, loaded from incidents/<id>/label.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from sickbay.evals.diagnosis import CATEGORIES, NOTHING_WRONG

LABEL_SCHEMA_VERSION = 1
STATUSES = ("draft", "reviewed")
SPLITS = ("dev", "heldout")

__all__ = [
    "CATEGORIES",
    "LABEL_SCHEMA_VERSION",
    "Label",
    "LabelError",
    "LabelEvidence",
    "load_label",
]

_FIELDS = frozenset(
    {
        "label_schema",
        "status",
        "id",
        "title",
        "summary",
        "fix",
        "split",
        "root_cause_category",
        "component",
        "also_accepted",
        "injection",
        "evidence",
        "leak_strings",
        "names",
    }
)
_EVIDENCE_FIELDS = frozenset({"source", "contains"})


class LabelError(ValueError):
    """A label file is unreadable or breaks label schema 1."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping with a repeated key (PyYAML keeps the last one)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


@dataclass(frozen=True)
class LabelEvidence:
    source: str
    contains: str


@dataclass(frozen=True)
class Label:
    label_schema: int
    status: str
    id: str
    title: str
    summary: str
    fix: str
    split: str
    root_cause_category: str
    component: str | None
    also_accepted: tuple[str, ...]
    injection: bool
    evidence: tuple[LabelEvidence, ...]
    leak_strings: tuple[str, ...]
    names: tuple[str, ...]


class _Checker:
    def __init__(self, path: Path) -> None:
        self.path = path

    def error(self, field: str, message: str) -> LabelError:
        return LabelError(f"{self.path}: {field}: {message}")

    def text(self, value: object, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise self.error(field, "must be a non-empty string")
        return value

    def choice(self, value: object, field: str, allowed: tuple[str, ...]) -> str:
        if not isinstance(value, str) or value not in allowed:
            raise self.error(field, f"must be one of {', '.join(allowed)}; got {value!r}")
        return value

    def texts(self, value: object, field: str) -> tuple[str, ...]:
        if not isinstance(value, list):
            raise self.error(field, "must be a list of strings")
        return tuple(self.text(v, f"{field}[{i}]") for i, v in enumerate(value))


def load_label(path: str | Path) -> Label:
    """Load and fully check one label.yaml; raise LabelError naming the file and field."""
    path = Path(path)
    check = _Checker(path)
    try:
        data: Any = yaml.load(path.read_bytes().decode("utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:  # e.g. a bad date
        raise check.error("(file)", f"cannot read: {exc}") from exc
    if not isinstance(data, dict):
        raise check.error("(file)", "must be a mapping")
    unknown = sorted(str(k) for k in data if k not in _FIELDS)
    if unknown:
        raise check.error(unknown[0], "unknown key")
    missing = sorted(_FIELDS - set(data))
    if missing:
        raise check.error(missing[0], "missing")

    schema = data["label_schema"]
    if type(schema) is not int or schema != LABEL_SCHEMA_VERSION:
        raise check.error("label_schema", f"must be {LABEL_SCHEMA_VERSION}; got {schema!r}")
    incident_id = check.text(data["id"], "id")
    if incident_id != path.parent.name:
        raise check.error("id", f"{incident_id!r} is not the directory name {path.parent.name!r}")
    category = check.choice(data["root_cause_category"], "root_cause_category", CATEGORIES)
    component = data["component"]
    if component is None:
        if category != NOTHING_WRONG:
            raise check.error("component", "may be null only when the category is nothing_wrong")
    else:
        component = check.text(component, "component")
    injection = data["injection"]
    if not isinstance(injection, bool):
        raise check.error("injection", "must be true or false")
    raw_evidence = data["evidence"]
    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise check.error("evidence", "must be a non-empty list")
    evidence = []
    for i, item in enumerate(raw_evidence):
        if not isinstance(item, dict) or set(item) != _EVIDENCE_FIELDS:
            raise check.error(f"evidence[{i}]", "must have exactly the keys source and contains")
        evidence.append(
            LabelEvidence(
                source=check.text(item["source"], f"evidence[{i}].source"),
                contains=check.text(item["contains"], f"evidence[{i}].contains"),
            )
        )
    return Label(
        label_schema=LABEL_SCHEMA_VERSION,
        status=check.choice(data["status"], "status", STATUSES),
        id=incident_id,
        title=check.text(data["title"], "title"),
        summary=check.text(data["summary"], "summary"),
        fix=check.text(data["fix"], "fix"),
        split=check.choice(data["split"], "split", SPLITS),
        root_cause_category=category,
        component=component,
        also_accepted=check.texts(data["also_accepted"], "also_accepted"),
        injection=injection,
        evidence=tuple(evidence),
        leak_strings=check.texts(data["leak_strings"], "leak_strings"),
        names=check.texts(data["names"], "names"),
    )
