"""list_units: rows of `systemctl list-units --all`."""

from __future__ import annotations

from sickbay.snapshot.commands import LIST_UNITS_ID
from sickbay.snapshot.hostview import HostView
from sickbay.tools.base import (
    ROW_LIMIT_DEFAULT,
    Args,
    Result,
    Tool,
    command_text,
    limit_property,
    rows_result,
    schema,
)

STATES = ["active", "inactive", "failed", "activating", "deactivating"]
TYPES = [
    "automount",
    "device",
    "mount",
    "path",
    "scope",
    "service",
    "slice",
    "socket",
    "swap",
    "target",
    "timer",
]


def parse_list_units(text: str) -> list[dict[str, str]]:
    rows = []
    for line in text.splitlines():
        cols = line.lstrip(" ●*").split(None, 4)
        if len(cols) < 4:
            continue
        rows.append(
            {
                "unit": cols[0],
                "load": cols[1],
                "active": cols[2],
                "sub": cols[3],
                "description": cols[4].rstrip() if len(cols) > 4 else "",
            }
        )
    return rows


def run(view: HostView, args: Args) -> Result:
    rows = parse_list_units(command_text(view, LIST_UNITS_ID))
    if "state" in args:
        rows = [r for r in rows if r["active"] == args["state"]]
    if "type" in args:
        rows = [r for r in rows if r["unit"].rsplit(".", 1)[-1] == args["type"]]
    if "name_contains" in args:
        rows = [r for r in rows if args["name_contains"] in r["unit"]]
    return rows_result("units", rows, args.get("limit", ROW_LIMIT_DEFAULT))


TOOL = Tool(
    name="list_units",
    description=(
        "List systemd units (systemctl list-units --all) with load, active and sub state. "
        "Filter by active state, unit type or a case-sensitive substring of the name."
    ),
    input_schema=schema(
        {
            "state": {"type": "string", "enum": STATES, "description": "ActiveState to match."},
            "type": {"type": "string", "enum": TYPES, "description": "Unit type suffix."},
            "name_contains": {
                "type": "string",
                "description": "Case-sensitive substring of the unit name.",
            },
            "limit": limit_property("units"),
        }
    ),
    run=run,
    rows_key="units",
)
