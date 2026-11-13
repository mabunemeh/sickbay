"""Tool definitions, argument validation, dispatch and the global result cap."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from sickbay.snapshot.hostview import HostView
from sickbay.tools import (
    cgroup_tree,
    disk_usage,
    journal_query,
    list_dir,
    list_units,
    process_limits,
    process_list,
    read_file,
    sockets,
    unit_status,
)
from sickbay.tools.base import (
    MAX_ARG_BYTES,
    MAX_RESULT_BYTES,
    Args,
    Result,
    Tool,
    ToolError,
    clip,
    echo,
    utf8,
)

# Bump on any change to a tool name, input schema or output shape.
TOOLSET_VERSION = 1

TOOLS: dict[str, Tool] = {
    t.TOOL.name: t.TOOL
    for t in (
        list_units,
        unit_status,
        journal_query,
        process_list,
        process_limits,
        cgroup_tree,
        disk_usage,
        sockets,
        read_file,
        list_dir,
    )
}


def tool_definitions() -> list[dict[str, Any]]:
    """name, description and input_schema of every tool, in a stable order."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in TOOLS.values()
    ]


def serialize(result: Result) -> str:
    """The deterministic JSON text of a result (sorted keys, compact, UTF-8 not escaped)."""
    return json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def result_size(result: Result) -> int:
    return len(serialize(result).encode("utf-8"))


def _check_value(key: str, spec: dict[str, Any], value: object) -> None:
    if spec["type"] == "string":
        if not isinstance(value, str):
            raise ToolError("invalid_argument", f"{key} must be a string")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise ToolError("invalid_argument", f"{key} is not valid UTF-8") from None
        if size > MAX_ARG_BYTES:
            raise ToolError("invalid_argument", f"{key} is longer than {MAX_ARG_BYTES} bytes")
        if any(unicodedata.category(c) == "Cc" for c in value):
            raise ToolError("invalid_argument", f"{key} contains a control character")
        if "pattern" in spec and re.fullmatch(spec["pattern"], value, re.ASCII) is None:
            raise ToolError("invalid_argument", f"{key} has an invalid format: {echo(value)}")
    elif spec["type"] == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ToolError("invalid_argument", f"{key} must be an integer")
        if not spec["minimum"] <= value <= spec["maximum"]:
            raise ToolError(
                "invalid_argument",
                f"{key} must be between {spec['minimum']} and {spec['maximum']}",
            )
    else:  # pragma: no cover - a schema bug, not a caller error
        raise AssertionError(f"unsupported schema type: {spec['type']}")
    if "enum" in spec and value not in spec["enum"]:
        raise ToolError("invalid_argument", f"{key} must be one of {', '.join(spec['enum'])}")


def validate(input_schema: dict[str, Any], args: object) -> Args:
    """Check args against the schema subset the tools use; return a plain copy."""
    if not isinstance(args, dict):
        raise ToolError("invalid_argument", "arguments must be a JSON object")
    props: dict[str, Any] = input_schema["properties"]
    for key in args:
        if not isinstance(key, str) or key not in props:
            raise ToolError("invalid_argument", f"unknown argument {echo(key)}")
    for key in input_schema.get("required", []):
        if key not in args:
            raise ToolError("invalid_argument", f"missing required argument {key}")
    for key, value in args.items():
        _check_value(key, props[key], value)
    return dict(args)


def _clip_values(value: Any, raw_keys: frozenset[str], top: bool = True) -> Any:
    if isinstance(value, str):
        return clip(value)
    if isinstance(value, list):
        return [_clip_values(v, raw_keys, False) for v in value]
    if isinstance(value, dict):
        return {
            utf8(k).decode("utf-8"): (
                utf8(v).decode("utf-8")
                if top and k in raw_keys and isinstance(v, str)
                else _clip_values(v, raw_keys, False)
            )
            for k, v in value.items()
        }
    return value


def _error(kind: str, message: str) -> Result:
    return {"error": {"kind": kind, "message": message}}


def enforce_cap(result: Result, rows_key: str | None, cap: int = MAX_RESULT_BYTES) -> Result:
    """Drop trailing rows until the serialised result fits in cap bytes, or give up."""
    if result_size(result) <= cap:
        return result
    rows = result.get(rows_key) if rows_key else None

    def keep(n: int) -> Result:
        assert isinstance(rows, list) and rows_key is not None
        return {**result, rows_key: rows[:n], "returned": n, "truncated": True}

    if isinstance(rows, list) and len(rows) > 1 and result_size(keep(1)) <= cap:
        lo, hi = 1, len(rows) - 1  # keep(lo) fits; find the largest n that does
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if result_size(keep(mid)) <= cap:
                lo = mid
            else:
                hi = mid - 1
        return keep(lo)
    return _error(
        "output_too_large",
        f"the result is over {cap} bytes; narrow the query (filters, limit, max_bytes)",
    )


def call_tool(view: HostView, name: object, args: object) -> Result:
    """Run one tool. Tool-level problems come back as {"error": {...}} results.

    A corrupt snapshot (SnapshotError) is not a tool result and propagates.
    """
    tool = TOOLS.get(name) if isinstance(name, str) else None
    if tool is None:
        return _error("unknown_tool", f"no tool named {echo(name)}")
    try:
        result = tool.run(view, validate(tool.input_schema, args))
    except ToolError as exc:
        return enforce_cap(_clip_values(exc.result(), frozenset()), None)
    return enforce_cap(_clip_values(result, tool.raw_keys), tool.rows_key)
