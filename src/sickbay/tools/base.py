"""Shared pieces of the tools: the Tool record, tool errors, caps and no-follow reads."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from sickbay.snapshot.hostview import HostView, Kind, UnknownCommandError

MAX_RESULT_BYTES = 8192
MAX_VALUE_BYTES = 2048
MAX_ARG_BYTES = 256
MAX_ECHO_BYTES = 64
ROW_LIMIT_DEFAULT = 50
ROW_LIMIT_MAX = 200

ErrorKind = Literal[
    "unknown_tool",
    "invalid_argument",
    "not_allowed",
    "not_found",
    "is_symlink",
    "is_directory",
    "not_a_directory",
    "unavailable",
    "output_too_large",
]

Args = dict[str, Any]
Result = dict[str, Any]


class ToolError(Exception):
    """A tool-level problem. call_tool turns it into an error result; it never escapes."""

    def __init__(self, kind: ErrorKind, message: str, **extra: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.extra = extra

    def result(self) -> Result:
        return {"error": {"kind": self.kind, "message": self.message}, **self.extra}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[HostView, Args], Result]
    rows_key: str | None = None
    # Top-level string keys exempt from the per-value cap (read_file's content has its own).
    raw_keys: frozenset[str] = field(default_factory=frozenset)


def schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        out["required"] = list(required)
    return out


def limit_property(what: str) -> dict[str, Any]:
    return {
        "type": "integer",
        "minimum": 1,
        "maximum": ROW_LIMIT_MAX,
        "description": f"Maximum {what} to return (default {ROW_LIMIT_DEFAULT}).",
    }


def utf8(text: str) -> bytes:
    """UTF-8 bytes of text; a lone surrogate becomes '?' so output is always valid UTF-8."""
    return text.encode("utf-8", "replace")


def clip(text: str, limit: int = MAX_VALUE_BYTES) -> str:
    """Cut text to at most `limit` UTF-8 bytes on a character boundary, marking the cut."""
    raw = utf8(text)
    if len(raw) <= limit:
        return raw.decode("utf-8")
    cut = limit
    while cut > 0 and raw[cut] & 0xC0 == 0x80:
        cut -= 1
    return f"{raw[:cut].decode('utf-8')}…[+{len(raw) - cut} bytes]"


def echo(value: object) -> str:
    """Caller input quoted for an error message, never more than MAX_ECHO_BYTES of it."""
    text = value if isinstance(value, str) else repr(value)
    return json.dumps(clip(text, MAX_ECHO_BYTES))


def decode(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def rows_result(key: str, rows: list[Any], limit: int) -> Result:
    shown = rows[:limit]
    return {
        key: shown,
        "total": len(rows),
        "returned": len(shown),
        "truncated": len(shown) < len(rows),
    }


def command_text(view: HostView, command_id: str) -> str:
    try:
        result = view.command(command_id)
    except UnknownCommandError:
        raise ToolError("unavailable", f"this snapshot has no '{command_id}' output") from None
    return decode(result.stdout)


def ancestors(path: str) -> list[str]:
    """Proper ancestors of an absolute normalised path, from "/" down."""
    if path == "/":
        return []
    parts = path[1:].split("/")
    return ["/"] + ["/" + "/".join(parts[:i]) for i in range(1, len(parts))]


def symlink_error(view: HostView, path: str) -> ToolError:
    return ToolError(
        "is_symlink",
        "path is a symlink; tools never follow links (request the target if it is allowed)",
        path=path,
        target=view.readlink(path),
    )


def kind_no_follow(view: HostView, path: str) -> Kind | None:
    """Entry type at path, refusing any path with a symlink (or non-directory) above it."""
    for parent in ancestors(path):
        k = view.kind(parent)
        if k == "symlink":
            raise symlink_error(view, parent)
        if k is None:
            return None
        if k != "dir":
            raise ToolError("not_a_directory", f"a parent of the path is not a directory: {parent}")
    return view.kind(path)


def read_regular(view: HostView, path: str) -> bytes | None:
    """Bytes of a regular file at an already allowlisted path; None if absent."""
    k = kind_no_follow(view, path)
    if k is None:
        return None
    if k == "symlink":
        raise symlink_error(view, path)
    if k == "dir":
        raise ToolError("is_directory", "path is a directory; use list_dir")
    if k != "file":
        raise ToolError("not_allowed", "path is not a regular file")
    try:
        return view.read_file(path)
    except FileNotFoundError:
        return None
