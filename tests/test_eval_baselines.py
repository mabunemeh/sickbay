"""The keyword and dump baselines, the Recorder, and their label independence."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from evalfakes import JournalView, entry

from sickbay.evals.baselines import keyword
from sickbay.evals.baselines.dump import (
    DUMP_MAX_BYTES,
    PROMPT_VERSION,
    build_dump,
    prompt_text,
    render_dump,
)
from sickbay.evals.baselines.keyword import RULES, KeywordPredictor
from sickbay.evals.diagnosis import CATEGORIES
from sickbay.evals.labels import load_label
from sickbay.evals.trace import Recorder, TraceEntry
from sickbay.snapshot import Snapshot
from sickbay.snapshot.hostview import CommandResult
from sickbay.tools import call_tool

REPO = Path(__file__).resolve().parent.parent
BASELINES = REPO / "src" / "sickbay" / "evals" / "baselines"


def test_baselines_never_see_labels() -> None:
    """No label import, no label file, no incident id or label name in any baseline file."""
    incidents = sorted(p for p in (REPO / "incidents").iterdir() if p.is_dir())
    assert incidents
    banned = set()
    for incident in incidents:
        banned.add(incident.name.lower())
        banned.update(n.lower() for n in load_label(incident / "label.yaml").names)
    files = sorted(p for p in BASELINES.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    assert {"keyword.py", "dump.py", "dump_v1.txt"} <= {p.name for p in files}
    for path in files:
        text = path.read_bytes().decode("utf-8")
        low = text.lower()
        for word in sorted(banned):
            assert word not in low, f"{word!r} in {path.name}"
        assert "label.yaml" not in low, path.name
        assert not re.search(r"^\s*(from|import)\s+\S*label", text, re.M), path.name
        assert "load_label" not in text, path.name


def test_recorder_numbers_calls_and_goes_through_call_tool() -> None:
    view = JournalView([entry("hello")])
    rec = Recorder(view)
    args = {"limit": 1}
    assert rec.call("journal_query", args)[0] == "c1"
    call_id, result = rec.call("read_file", {"path": "/etc/shadow"})
    assert call_id == "c2" and result["error"]["kind"] == "not_allowed"
    assert rec.call("nope", {})[1]["error"]["kind"] == "unknown_tool"
    assert [c["call_id"] for c in rec.trace] == ["c1", "c2", "c3"]
    assert rec.trace[0]["result"] == call_tool(view, "journal_query", {"limit": 1})
    args["limit"] = 99
    assert rec.trace[0]["args"] == {"limit": 1}


def test_keyword_fixed_trace_and_first_rule_wins() -> None:
    view = JournalView(
        [
            entry(
                "svc.service: Scheduled restart job, restart counter is at 3.", UNIT="svc.service"
            ),
            entry("open: Too many open files", 2_000_000, _SYSTEMD_UNIT="api.service"),
        ]
    )
    pred = KeywordPredictor().predict(view)
    assert pred.outcome == "ok" and pred.diagnosis is not None
    assert [(c["tool"], c["args"]) for c in pred.trace] == [
        *(("journal_query", {"contains": s, "limit": keyword.QUERY_LIMIT}) for s, _ in RULES),
        ("list_units", {}),
    ]
    d = pred.diagnosis
    assert (d.root_cause_category, d.component, d.confidence) == ("fd_limit", "api.service", 0.5)
    assert [(e.call_id, e.quote) for e in d.evidence] == [("c1", "open: Too many open files")]
    assert pred.usage == {
        "input_tokens": None,
        "output_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
        "api_calls": 0,
        "cost_usd": 0.0,
        "latency_s": None,
    }
    assert pred.detail is None


def test_keyword_component_from_message_token_and_clipped_quote() -> None:
    message = "web.socket: Address already in use " + "x" * 300
    pred = KeywordPredictor().predict(JournalView([entry(message)]))
    assert pred.diagnosis is not None
    assert pred.diagnosis.root_cause_category == "port_conflict"
    assert pred.diagnosis.component == "web.socket"
    assert pred.diagnosis.evidence[0].quote == message[:200]
    assert pred.diagnosis.evidence[0].call_id == "c4"


def test_keyword_no_hit_is_nothing_wrong() -> None:
    pred = KeywordPredictor().predict(JournalView([entry("all quiet")]))
    assert pred.diagnosis is not None
    assert (pred.diagnosis.root_cause_category, pred.diagnosis.component) == ("nothing_wrong", "")
    assert pred.diagnosis.evidence == ()


def test_keyword_hit_without_unit_is_invalid_output_not_a_crash() -> None:
    pred = KeywordPredictor().predict(JournalView([entry("disk: No space left on device")]))
    assert pred.outcome == "invalid_output" and pred.diagnosis is None
    assert len(pred.trace) == len(RULES) + 1


def test_keyword_rules_cover_known_categories() -> None:
    assert {c for _, c in RULES} <= set(CATEGORIES)
    assert set(keyword.FIXES) <= set(CATEGORIES)


def test_dump_recipe_and_unit_status_for_sick_units() -> None:
    trace, text = build_dump(JournalView([entry("boom", PRIORITY="3")]))
    assert [c["tool"] for c in trace] == [
        "list_units",
        "list_units",
        "journal_query",
        "journal_query",
        "process_list",
        "cgroup_tree",
        "disk_usage",
        "sockets",
        "unit_status",
        "unit_status",
    ]
    assert trace[0]["args"] == {"state": "failed", "limit": 200}
    assert trace[1]["args"] == {"state": "activating", "limit": 200}
    assert trace[2]["args"] == {"priority_max": 4, "limit": 200}
    assert trace[3]["args"] == {"order": "newest", "limit": 200}
    assert [c["args"] for c in trace[8:]] == [{"unit": "aa.socket"}, {"unit": "app.service"}]
    assert text.startswith('### c1 list_units {"limit":200,"state":"failed"}\n{')
    assert '### c3 journal_query {"limit":200,"priority_max":4}\n' in text
    assert text.endswith("### dump_truncated false\n")


@pytest.mark.parametrize(
    ("incident", "unit"),
    [
        ("memory-ceiling-parent-slice", "report-builder.service"),
        ("restart-loop", "quote-sync.service"),
    ],
)
def test_dump_on_committed_incidents_reaches_the_sick_unit(incident: str, unit: str) -> None:
    trace, text = build_dump(Snapshot.open(REPO / "incidents" / incident / "snapshot"))
    calls = [c for c in trace if c["tool"] == "unit_status" and c["args"] == {"unit": unit}]
    assert len(calls) == 1 and "error" not in calls[0]["result"]
    assert f'unit_status {{"unit":"{unit}"}}' in text


def test_dump_unit_status_is_capped_at_5() -> None:
    rows = "".join(f"  u{i}.service loaded failed failed U\n" for i in range(7))

    class Many(JournalView):
        def command(self, command_id: str) -> CommandResult:
            return CommandResult(("systemctl",), 0, rows.encode())

    trace, _ = build_dump(Many([]))
    targets = [c["args"]["unit"] for c in trace if c["tool"] == "unit_status"]
    assert targets == [f"u{i}.service" for i in range(5)]


def _fake_trace(n: int, size: int) -> list[TraceEntry]:
    return [
        {"call_id": f"c{i}", "tool": "t", "args": {}, "result": {"v": "x" * size}}
        for i in range(1, n + 1)
    ]


def test_dump_cap_drops_whole_calls_from_the_end() -> None:
    text, truncated, dropped = render_dump(_fake_trace(5, 20_000))
    assert truncated and dropped == ["c3", "c4", "c5"]
    assert len(text.encode("utf-8")) <= DUMP_MAX_BYTES
    assert text.endswith("### dump_truncated true dropped c3 c4 c5\n")
    assert "### c2 t {}" in text and "### c3" not in text
    small, truncated, dropped = render_dump(_fake_trace(2, 10))
    assert not truncated and dropped == []
    assert render_dump(_fake_trace(1, 100), max_bytes=50)[2] == ["c1"]


def test_dump_prompt() -> None:
    assert PROMPT_VERSION == "dump-v1"
    prompt = prompt_text()
    assert "untrusted" in prompt.lower() and "call_id" in prompt
    listed = re.findall(r"[a-z_]+", prompt.split("exactly one of", 1)[1].split("- component")[0])
    assert tuple(listed) == CATEGORIES


def test_baselines_do_not_load_the_label_module_even_indirectly() -> None:
    code = (
        "import sys, sickbay.evals.baselines.keyword, sickbay.evals.baselines.dump; "
        "print('sickbay.evals.labels' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_keyword_device_token_from_message() -> None:
    message = "dev-sdb1.device: No space left on device"
    pred = KeywordPredictor().predict(JournalView([entry(message)]))
    assert pred.diagnosis is not None and pred.diagnosis.component == "dev-sdb1.device"


def test_keyword_fixes_are_generic() -> None:
    assert "slice" not in keyword.FIXES["memory_limit"].lower()
    assert set(keyword.FIXES) == {c for _, c in RULES} | {"nothing_wrong"}


def test_dump_cap_counts_bytes_not_characters() -> None:
    trace: list[TraceEntry] = [
        {"call_id": f"c{i}", "tool": "t", "args": {}, "result": {"v": "\u00e9" * 1000}}
        for i in (1, 2)
    ]
    full, truncated, dropped = render_dump(trace, max_bytes=10_000)
    size = len(full.encode("utf-8"))
    assert not truncated and size > len(full)  # 2 bytes per character
    # A cap at the character count but below the byte count must drop a call.
    text, truncated, dropped = render_dump(trace, max_bytes=len(full))
    assert truncated and dropped == ["c2"] and len(text.encode("utf-8")) <= len(full)


def test_dump_cap_equal_to_the_full_size_keeps_everything() -> None:
    trace = _fake_trace(3, 100)
    full, _, _ = render_dump(trace)
    size = len(full.encode("utf-8"))
    assert render_dump(trace, max_bytes=size) == (full, False, [])
    assert render_dump(trace, max_bytes=size - 1)[2] == ["c3"]
