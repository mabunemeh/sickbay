"""sockets: listening TCP/UDP sockets from `ss -tulnpH`, columns verbatim."""

from __future__ import annotations

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


def parse_ss(text: str) -> list[dict[str, str]]:
    """Columns: Netid State Recv-Q Send-Q Local Peer [Process]."""
    rows = []
    for line in text.splitlines():
        cols = line.split(None, 6)
        if len(cols) < 6:
            continue
        rows.append(
            {
                "netid": cols[0],
                "state": cols[1],
                "local": cols[4],
                "peer": cols[5],
                "process": cols[6].rstrip() if len(cols) > 6 else "",
            }
        )
    return rows


def run(view: HostView, args: Args) -> Result:
    rows = parse_ss(command_text(view, "ss"))
    if "port" in args:
        port = str(args["port"])
        rows = [r for r in rows if r["local"].rpartition(":")[2] == port]
    return rows_result("sockets", rows, args.get("limit", ROW_LIMIT_DEFAULT))


TOOL = Tool(
    name="sockets",
    description=(
        "Listening TCP and UDP sockets (ss -tulnp): netid, state, local and peer address, and "
        "the owning process. Filter by local port."
    ),
    input_schema=schema(
        {
            "port": {
                "type": "integer",
                "minimum": 0,
                "maximum": 65535,
                "description": "Only sockets whose local port is this.",
            },
            "limit": limit_property("sockets"),
        }
    ),
    run=run,
    rows_key="sockets",
)
