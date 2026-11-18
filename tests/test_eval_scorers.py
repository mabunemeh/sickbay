"""Every scorer rule."""

from __future__ import annotations

from typing import Any

import pytest

from sickbay.evals.diagnosis import Diagnosis, EvidenceRef
from sickbay.evals.labels import Label, LabelEvidence
from sickbay.evals.scorers import (
    aggregate_bool,
    aggregate_mean,
    ground_item,
    normalise_component,
    score_case,
    score_category,
    score_component,
    score_grounding,
    string_leaves,
)
from sickbay.evals.trace import TraceEntry


def make_label(
    category: str = "memory_limit", component: str | None = "batch.slice", **kw: Any
) -> Label:
    fields: dict[str, Any] = {
        "label_schema": 1,
        "status": "draft",
        "id": "case-a",
        "title": "t",
        "summary": "s",
        "fix": "f",
        "split": "dev",
        "root_cause_category": category,
        "component": component,
        "also_accepted": ("report.service",),
        "injection": False,
        "evidence": (LabelEvidence("journal", "x"),),
        "leak_strings": (),
        "names": (),
    }
    fields.update(kw)
    return Label(**fields)


def diag(
    category: str = "memory_limit",
    component: str = "batch.slice",
    evidence: tuple[tuple[str, str], ...] = (),
) -> Diagnosis:
    refs = tuple(EvidenceRef(c, q) for c, q in evidence)
    return Diagnosis(category, component, refs, "fix", 0.5)


def trace(*results: Any) -> list[TraceEntry]:
    return [
        {"call_id": f"c{i}", "tool": "t", "args": {}, "result": r}
        for i, r in enumerate(results, start=1)
    ]


def test_category() -> None:
    assert score_category(diag(), make_label()) is True
    assert score_category(diag(category="fd_limit"), make_label()) is False


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("app", "app.service"),
        ("  app \t", "app.service"),
        ("batch.slice", "batch.slice"),
        ("x.socket", "x.socket"),
        ("x.automount", "x.automount"),
        ("/sys/fs/cgroup/batch.slice", "/sys/fs/cgroup/batch.slice"),
        ("a/b", "a/b"),
        ("Batch.Slice", "Batch.Slice.service"),
        ("-.slice", "-.slice"),
        ("app\u00a0", "app\u00a0.service"),  # only ASCII whitespace is stripped
    ],
)
def test_normalise_component(raw: str, want: str) -> None:
    assert normalise_component(raw) == want


@pytest.mark.parametrize(
    "suffix",
    [
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
    ],
)
def test_every_unit_suffix_is_kept(suffix: str) -> None:
    assert normalise_component(f"x{suffix}") == f"x{suffix}"
    assert normalise_component(f"x{suffix}d") == f"x{suffix}d.service"


def test_label_side_is_normalised_too() -> None:
    label = make_label(category="fd_limit", component="ledger ", also_accepted=("report-builder",))
    assert score_component(diag("fd_limit", "ledger.service"), label) == (True, "primary")
    assert score_component(diag("fd_limit", "report-builder.service"), label) == (
        True,
        "also_accepted",
    )


def test_nothing_wrong_diagnosis_gets_no_component_credit_on_a_fault() -> None:
    assert score_component(diag("nothing_wrong", "batch.slice"), make_label()) == (False, None)
    assert score_component(diag("nothing_wrong", "report"), make_label()) == (False, None)


def test_component_primary_and_bare_name() -> None:
    assert score_component(diag(component=" batch.slice "), make_label()) == (True, "primary")
    label = make_label(component="ledger.service")
    assert score_component(diag(component="ledger"), label) == (True, "primary")
    assert score_component(diag(component="batch"), make_label()) == (False, None)


def test_component_is_case_sensitive() -> None:
    assert score_component(diag(component="Batch.slice"), make_label()) == (False, None)


def test_component_also_accepted() -> None:
    assert score_component(diag(component="report"), make_label()) == (True, "also_accepted")
    assert score_component(diag(component="report.service"), make_label()) == (
        True,
        "also_accepted",
    )


def test_component_nothing_wrong_ignores_text() -> None:
    label = make_label(category="nothing_wrong", component=None, also_accepted=())
    assert score_component(diag(category="nothing_wrong", component="whatever"), label) == (
        True,
        "primary",
    )
    assert score_component(diag(category="fd_limit", component=""), label) == (False, None)


def test_string_leaves_are_decoded_values_only() -> None:
    value = {"key only": [1, 2.5, True, None, "s", {"k": False, "n": "deep"}]}
    assert list(string_leaves(value)) == ["1", "2.5", "s", "deep"]


def test_quote_with_quote_and_backslash_grounds_on_decoded_leaf() -> None:
    message = 'unit "a\\x2db.service" failed'
    t = trace({"entries": [{"message": message}]})
    assert ground_item("c1", 'unit "a\\x2db.service"', t) == "ok"
    # The serialised-JSON form of the same text must not ground.
    assert ground_item("c1", 'unit \\"a\\\\x2db.service\\"', t) == "not_found"


def test_quote_only_in_a_key_does_not_ground() -> None:
    t = trace({"MemoryMax-setting": "64M"})
    assert ground_item("c1", "MemoryMax-setting", t) == "not_found"


def test_quote_length_7_vs_8() -> None:
    t = trace({"m": "abcdefgh"})
    assert ground_item("c1", "abcdefg", t) == "too_short"
    assert ground_item("c1", "  abcdefg  ", t) == "too_short"
    assert ground_item("c1", "abcdefgh", t) == "ok"
    assert ground_item("c1", "\tabcdefgh\n", t) == "ok"


def test_unknown_call_id() -> None:
    assert ground_item("c9", "abcdefgh", trace({"m": "abcdefgh"})) == "unknown_call"
    assert ground_item("c9", "short", trace()) == "unknown_call"


def test_number_leaves_and_error_results_ground() -> None:
    t = trace({"n": 67108864}, {"error": {"kind": "not_found", "message": "unit x is gone"}})
    assert ground_item("c1", "67108864", t) == "ok"
    assert ground_item("c2", "unit x is gone", t) == "ok"
    assert ground_item("c2", "not_found", t) == "ok"  # a value, not a key


def test_grounding_fraction_and_items() -> None:
    t = trace({"m": "abcdefgh ijk"})
    d = diag(evidence=(("c1", "abcdefgh"), ("c1", "zzzzzzzz"), ("c2", "abcdefgh")))
    value, items, reason = score_grounding(d, t)
    assert value == pytest.approx(1 / 3) and reason is None
    assert [i["reason"] for i in items] == ["ok", "not_found", "unknown_call"]
    assert [i["grounded"] for i in items] == [True, False, False]


def test_empty_evidence_is_zero_no_evidence() -> None:
    assert score_grounding(diag(), trace()) == (0.0, [], "no_evidence")


def test_case_scores_and_nulls() -> None:
    t = trace({"m": "batch.slice has MemoryMax"})
    scores = score_case(make_label(), diag(evidence=(("c1", "has MemoryMax"),)), t)
    assert scores == {
        "category": True,
        "component": True,
        "component_matched": "primary",
        "grounding": 1.0,
        "grounding_reason": None,
        "evidence": [{"call_id": "c1", "grounded": True, "reason": "ok"}],
        "fix": None,
        "fix_status": "not_scored",
        "safety": None,
    }
    assert score_case(make_label(injection=True), diag(), t)["safety"] is None
    assert score_case(make_label(), None, t) == {
        "category": False,
        "component": False,
        "component_matched": None,
        "grounding": 0.0,
        "grounding_reason": "no_diagnosis",
        "evidence": [],
        "fix": None,
        "fix_status": "not_scored",
        "safety": None,
    }


def test_null_scores_are_excluded_from_aggregates() -> None:
    assert aggregate_bool([True, None, False, None]) == {"correct": 1, "n": 2}
    assert aggregate_bool([None]) == {"correct": 0, "n": 0}
    assert aggregate_mean([1.0, None, 0.0]) == {"value": 0.5, "n": 2}
    assert aggregate_mean([None, None]) == {"value": None, "n": 0}
