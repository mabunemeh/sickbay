"""process_list: rows of the captured `ps` output, columns verbatim."""

from __future__ import annotations

from sickbay.snapshot.commands import PS_ID
from sickbay.snapshot.hostview import HostView
from sickbay.tools.base import (
    ROW_LIMIT_DEFAULT,
    Args,
    Result,
    Tool,
    clip,
    command_text,
    limit_property,
    rows_result,
    schema,
)

MAX_ARGS_BYTES = 512
COLUMNS = ("pid", "ppid", "user", "stat", "rss", "vsz", "nlwp", "etimes", "args")


def _int(text: str) -> int:
    return int(text) if text.isascii() and text.isdigit() else -1


def parse_ps(text: str) -> list[dict[str, str]]:
    rows = []
    for line in text.splitlines():
        cols = line.split(None, len(COLUMNS) - 1)
        if len(cols) < len(COLUMNS) - 1:
            continue
        cols += [""] * (len(COLUMNS) - len(cols))
        rows.append(dict(zip(COLUMNS, [*cols[:-1], cols[-1].rstrip()], strict=True)))
    return rows


def run(view: HostView, args: Args) -> Result:
    rows = parse_ps(command_text(view, PS_ID))
    if "name_contains" in args:
        rows = [r for r in rows if args["name_contains"] in r["args"]]
    if args.get("sort", "pid") == "rss":
        rows.sort(key=lambda r: (-_int(r["rss"]), _int(r["pid"])))
    else:
        rows.sort(key=lambda r: _int(r["pid"]))
    result = rows_result("processes", rows, args.get("limit", ROW_LIMIT_DEFAULT))
    for row in result["processes"]:
        row["args"] = clip(row["args"], MAX_ARGS_BYTES)
    return result


TOOL = Tool(
    name="process_list",
    description=(
        "List processes (ps: pid, ppid, user, stat, rss and vsz in KiB, nlwp threads, etimes "
        "seconds, args). Sort by pid or by rss (largest first); filter by a substring of args."
    ),
    input_schema=schema(
        {
            "sort": {
                "type": "string",
                "enum": ["pid", "rss"],
                "description": "pid ascending (default) or rss descending.",
            },
            "name_contains": {
                "type": "string",
                "description": "Case-sensitive substring of the command line.",
            },
            "limit": limit_property("processes"),
        }
    ),
    run=run,
    rows_key="processes",
)
