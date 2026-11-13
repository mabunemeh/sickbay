"""process_limits: /proc/<pid>/limits rows and the number of open file descriptors."""

from __future__ import annotations

from sickbay.snapshot.hostview import HostView
from sickbay.tools.base import (
    Args,
    Result,
    Tool,
    ToolError,
    decode,
    echo,
    kind_no_follow,
    read_regular,
    rows_result,
    schema,
    symlink_error,
)
from sickbay.tools.paths import PID_PATTERN, pid_path

# The kernel prints "%-25s %-20s %-20s %-10s".
_SPANS = ((0, 26), (26, 47), (47, 68), (68, None))


def parse_limits(text: str) -> list[dict[str, str]]:
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("Limit "):
            continue
        cells = [line[a:b].strip() for a, b in _SPANS]
        rows.append(dict(zip(("limit", "soft", "hard", "units"), cells, strict=True)))
    return rows


def open_fds(view: HostView, path: str) -> int | None:
    """Lines of the snapshot's synthetic fd listing, or entries of a live /proc/<pid>/fd."""
    kind = kind_no_follow(view, path)
    if kind is None:
        return None
    if kind == "symlink":
        raise symlink_error(view, path)
    if kind == "dir":
        return len(view.list_dir(path))
    data = read_regular(view, path)
    return None if data is None else sum(1 for line in data.splitlines() if line.strip())


def run(view: HostView, args: Args) -> Result:
    limits_path = pid_path(args["pid"], "limits")
    fd_path = pid_path(args["pid"], "fd")
    limits = read_regular(view, limits_path)
    fds = open_fds(view, fd_path)
    if limits is None and fds is None:
        raise ToolError("not_found", f"no process {echo(args['pid'])} in this snapshot")
    rows = parse_limits(decode(limits)) if limits is not None else []
    result = rows_result("limits", rows, len(rows))
    result["pid"] = args["pid"]
    if fds is not None:
        result["open_fds"] = fds
    return result


TOOL = Tool(
    name="process_limits",
    description=(
        "Resource limits of one process (/proc/<pid>/limits: soft, hard, units per limit) and "
        "its number of open file descriptors (open_fds)."
    ),
    input_schema=schema(
        {
            "pid": {
                "type": "string",
                "pattern": PID_PATTERN,
                "description": "Process id as decimal digits, e.g. '1234'.",
            }
        },
        required=("pid",),
    ),
    run=run,
    rows_key="limits",
)
