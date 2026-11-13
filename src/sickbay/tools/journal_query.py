"""journal_query: filtered journal entries, MESSAGE verbatim (cut at 1,024 bytes)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sickbay.snapshot.hostview import HostView
from sickbay.tools.base import (
    ROW_LIMIT_DEFAULT,
    Args,
    Result,
    Tool,
    clip,
    limit_property,
    rows_result,
    schema,
)
from sickbay.tools.paths import UNIT_PATTERN, check_unit

MAX_MESSAGE_BYTES = 1024
# A unit filter matches any of these (as `journalctl -u` does, plus the user-unit fields).
UNIT_FIELDS = (
    "UNIT",
    "USER_UNIT",
    "OBJECT_SYSTEMD_UNIT",
    "COREDUMP_UNIT",
    "_SYSTEMD_UNIT",
    "_SYSTEMD_USER_UNIT",
)


def field_text(entry: dict[str, Any], key: str) -> str | None:
    """A journal field as text: binary fields (byte arrays) are decoded as UTF-8."""
    value = entry.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(b, int) and 0 <= b < 256 for b in value):
        return bytes(value).decode("utf-8", "replace")
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def iso_utc(usec: str) -> str | None:
    if not usec.isascii() or not usec.isdigit():
        return None
    seconds, micros = divmod(int(usec), 1_000_000)
    try:
        moment = datetime.fromtimestamp(seconds, UTC).replace(microsecond=micros)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row(entry: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    usec = field_text(entry, "__REALTIME_TIMESTAMP")
    if usec is not None:
        row["time"] = {"usec": usec}
        utc = iso_utc(usec)
        if utc is not None:
            row["time"]["utc"] = utc
    unit = next((u for k in UNIT_FIELDS if (u := field_text(entry, k)) is not None), None)
    for key, value in (
        ("unit", unit),
        ("identifier", field_text(entry, "SYSLOG_IDENTIFIER")),
        ("pid", field_text(entry, "_PID")),
        ("priority", field_text(entry, "PRIORITY")),
    ):
        if value is not None:
            row[key] = value
    row["message"] = clip(field_text(entry, "MESSAGE") or "", MAX_MESSAGE_BYTES)
    return row


def _sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, int]:
    usec = field_text(item[1], "__REALTIME_TIMESTAMP") or ""
    return (int(usec) if usec.isascii() and usec.isdigit() else 0, item[0])


def run(view: HostView, args: Args) -> Result:
    unit = check_unit(args["unit"]) if "unit" in args else None
    priority_max = args.get("priority_max")
    contains = args.get("contains")
    matched: list[tuple[int, dict[str, Any]]] = []
    for index, entry in enumerate(view.journal()):
        if unit is not None and unit not in {field_text(entry, k) for k in UNIT_FIELDS}:
            continue
        if priority_max is not None:
            prio = field_text(entry, "PRIORITY") or ""
            if not (prio.isascii() and prio.isdigit() and int(prio) <= priority_max):
                continue
        if contains is not None and contains not in (field_text(entry, "MESSAGE") or ""):
            continue
        matched.append((index, entry))
    matched.sort(key=_sort_key, reverse=args.get("order", "newest") == "newest")
    limit = args.get("limit", ROW_LIMIT_DEFAULT)
    result = rows_result("entries", matched, limit)
    result["entries"] = [_row(entry) for _, entry in result["entries"]]
    return result


TOOL = Tool(
    name="journal_query",
    description=(
        "Search the systemd journal. Filter by unit, maximum priority (0 emerg .. 7 debug) and a "
        "literal case-sensitive substring of MESSAGE. Newest first by default."
    ),
    input_schema=schema(
        {
            "unit": {
                "type": "string",
                "pattern": UNIT_PATTERN,
                "description": "Unit name; matches UNIT, _SYSTEMD_UNIT and related fields.",
            },
            "priority_max": {
                "type": "integer",
                "minimum": 0,
                "maximum": 7,
                "description": "Only entries with PRIORITY at most this (3 = err and worse).",
            },
            "contains": {
                "type": "string",
                "description": "Literal, case-sensitive substring of MESSAGE (no regex).",
            },
            "limit": limit_property("entries"),
            "order": {
                "type": "string",
                "enum": ["newest", "oldest"],
                "description": "Sort order by time (default newest).",
            },
        }
    ),
    run=run,
    rows_key="entries",
)
