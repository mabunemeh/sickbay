"""Traces: every tool call a predictor makes, in order, with ids c1, c2, ..."""

from __future__ import annotations

import copy
from typing import Any

from sickbay.snapshot.hostview import HostView
from sickbay.tools import call_tool
from sickbay.tools.base import Result

# One call: {"call_id": "c1", "tool": name, "args": {...}, "result": {...}}
TraceEntry = dict[str, Any]


class Recorder:
    """Runs tools through call_tool (so caps and the allowlist apply) and records each call."""

    def __init__(self, view: HostView) -> None:
        self.view = view
        self.trace: list[TraceEntry] = []

    def call(self, name: object, args: object) -> tuple[str, Result]:
        """Any name and args (a model's tool_use may hold anything): call_tool turns an
        unknown name or a non-object input into an error result, which still gets an id."""
        call_id = f"c{len(self.trace) + 1}"
        result = call_tool(self.view, name, args)
        self.trace.append(
            {
                "call_id": call_id,
                "tool": name,
                "args": copy.deepcopy(args),
                "result": result,
            }
        )
        return call_id, result
