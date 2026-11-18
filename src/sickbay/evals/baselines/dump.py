"""The dump baseline: a fixed recipe of tool calls, rendered as one text for a model.

Batch 3 builds the dump only (`sickbay eval --baseline dump --dry-run`); a model scores
it from batch 4. The recipe, all through the Recorder:

1. list_units state=failed, then list_units state=activating (limit 200 each);
2. journal_query, priority 0-4, limit 200;
3. journal_query, newest entries, limit 200;
4. process_list, cgroup_tree, disk_usage, sockets (default arguments);
5. unit_status for each unit those two calls returned, de-duplicated, sorted, at most 5.

Text: per call a header `### <call_id> <tool> <args json>` and the serialised result. The
total is capped at DUMP_MAX_BYTES by dropping whole calls from the end; the last line
always records `### dump_truncated <true|false>` and any dropped call ids.
"""

from __future__ import annotations

from importlib import resources

from sickbay.evals.trace import Recorder, TraceEntry
from sickbay.snapshot.hostview import HostView
from sickbay.tools import serialize
from sickbay.tools.base import ROW_LIMIT_MAX

PROMPT_VERSION = "dump-v1"
DUMP_MAX_BYTES = 60_000
MAX_UNIT_STATUS = 5
_UNIT_STATES = ("failed", "activating")


def prompt_text() -> str:
    """The dump-v1 system prompt."""
    return (
        resources.files("sickbay.evals.baselines")
        .joinpath("prompts", "dump_v1.txt")
        .read_bytes()
        .decode("utf-8")
    )


def render_call(call: TraceEntry) -> str:
    return f"### {call['call_id']} {call['tool']} {serialize(call['args'])}\n" + (
        serialize(call["result"]) + "\n"
    )


def _trailer(dropped: list[str]) -> str:
    if not dropped:
        return "### dump_truncated false\n"
    return f"### dump_truncated true dropped {' '.join(dropped)}\n"


def render_dump(
    trace: list[TraceEntry], max_bytes: int = DUMP_MAX_BYTES
) -> tuple[str, bool, list[str]]:
    """(text, dump_truncated, dropped call ids); text is at most max_bytes of UTF-8."""
    parts = [render_call(call) for call in trace]
    keep = len(parts)
    while keep > 0:
        dropped = [call["call_id"] for call in trace[keep:]]
        text = "".join(parts[:keep]) + _trailer(dropped)
        if len(text.encode("utf-8")) <= max_bytes:
            return text, bool(dropped), dropped
        keep -= 1
    dropped = [call["call_id"] for call in trace]
    return _trailer(dropped), bool(dropped), dropped


def build_dump(view: HostView) -> tuple[list[TraceEntry], str]:
    """Run the fixed recipe; return the full trace and the capped dump text."""
    recorder = Recorder(view)
    listings = [
        recorder.call("list_units", {"state": state, "limit": ROW_LIMIT_MAX})[1]
        for state in _UNIT_STATES
    ]
    recorder.call("journal_query", {"priority_max": 4, "limit": ROW_LIMIT_MAX})
    recorder.call("journal_query", {"order": "newest", "limit": ROW_LIMIT_MAX})
    for tool in ("process_list", "cgroup_tree", "disk_usage", "sockets"):
        recorder.call(tool, {})
    sick = sorted(
        {
            row["unit"]
            for listing in listings
            if isinstance(rows := listing.get("units"), list)
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("unit"), str)
        }
    )
    for unit in sick[:MAX_UNIT_STATUS]:
        recorder.call("unit_status", {"unit": unit})
    text, _, _ = render_dump(recorder.trace)
    return recorder.trace, text
