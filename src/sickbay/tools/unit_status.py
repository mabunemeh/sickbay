"""unit_status: one unit's `systemctl show` properties, values verbatim."""

from __future__ import annotations

from sickbay.snapshot.commands import SHOW_ID
from sickbay.snapshot.hostview import HostView
from sickbay.tools.base import Args, Result, Tool, ToolError, command_text, echo, schema
from sickbay.tools.paths import UNIT_PATTERN, check_unit

Properties = dict[str, str | list[str]]


def parse_show(text: str) -> list[Properties]:
    """Blocks of Key=Value lines separated by blank lines.

    A key seen once maps to its raw value; a repeated key (several ExecStart= lines, for
    example) maps to the list of its raw values in output order.
    """
    blocks: list[Properties] = []
    current: Properties = {}
    for line in text.split("\n"):
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        seen = current.get(key)
        if seen is None:
            current[key] = value
        elif isinstance(seen, list):
            seen.append(value)
        else:
            current[key] = [seen, value]
    if current:
        blocks.append(current)
    return blocks


def run(view: HostView, args: Args) -> Result:
    unit = check_unit(args["unit"])
    for block in parse_show(command_text(view, SHOW_ID)):
        if block.get("Id") == unit:
            return {"unit": unit, "properties": block}
    raise ToolError(
        "not_found",
        f"unit {echo(unit)} is not in the systemctl show output "
        "(only loaded services, slices and scopes are captured)",
    )


TOOL = Tool(
    name="unit_status",
    description=(
        "Show one unit's systemctl show properties (state, Result, MainPID, NRestarts, limits, "
        "memory, cgroup, ExecStart, ...) as raw Key=Value strings."
    ),
    input_schema=schema(
        {
            "unit": {
                "type": "string",
                "pattern": UNIT_PATTERN,
                "description": "Full unit name, e.g. 'nginx.service'.",
            }
        },
        required=("unit",),
    ),
    run=run,
)
