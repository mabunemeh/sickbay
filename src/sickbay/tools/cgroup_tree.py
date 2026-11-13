"""cgroup_tree: memory and pids files of a cgroup and its descendants, raw file text."""

from __future__ import annotations

from typing import Any

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
from sickbay.tools.paths import CGROUP_ROOT, cgroup_path, check_readable, join

MAX_NODES = 100
FILES = (
    "memory.current",
    "memory.max",
    "memory.high",
    "pids.current",
    "pids.max",
    "memory.events",
)


def _node(view: HostView, host: str, cg: str) -> dict[str, Any]:
    node: dict[str, Any] = {"path": cg}
    for name in FILES:
        if view.kind(join(host, name)) == "file":
            data = read_regular(view, check_readable(join(host, name)))
            if data is not None:
                node[name] = decode(data)
    if view.kind(join(host, "cgroup.procs")) == "file":
        procs = read_regular(view, check_readable(join(host, "cgroup.procs")))
        if procs is not None:
            node["procs_count"] = sum(1 for line in procs.splitlines() if line.strip())
    return node


def run(view: HostView, args: Args) -> Result:
    cg = args.get("path", "/")
    host = cgroup_path(cg)
    kind = kind_no_follow(view, host)
    if kind is None:
        raise ToolError("not_found", f"no cgroup {echo(cg)} in this snapshot")
    if kind == "symlink":
        raise symlink_error(view, host)
    if kind != "dir":
        raise ToolError("not_a_directory", f"not a cgroup directory: {echo(cg)}")
    depth = args.get("depth", 1)
    found: list[tuple[str, str]] = []
    level = [(host, cg)]
    for d in range(depth + 1):
        found += level
        if d == depth:
            break
        nxt = []
        for parent, _ in level:
            for name in view.list_dir(parent):
                child = check_readable(join(parent, name))
                if view.kind(child) == "dir":  # files are read per node; links never followed
                    nxt.append((child, child[len(CGROUP_ROOT) :]))
        level = nxt
    result = rows_result("nodes", found, MAX_NODES)
    result["nodes"] = [_node(view, h, c) for h, c in result["nodes"]]
    return result


TOOL = Tool(
    name="cgroup_tree",
    description=(
        "Walk the cgroup v2 tree from a cgroup path ('/' is the root, e.g. '/system.slice') to "
        "the given depth: memory.current/max/high, pids.current/max, memory.events (raw file "
        "text; absent files omitted) and the number of processes in cgroup.procs."
    ),
    input_schema=schema(
        {
            "path": {
                "type": "string",
                "description": "Absolute cgroup path, default '/'.",
            },
            "depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3,
                "description": "Levels below path to include (default 1).",
            },
        }
    ),
    run=run,
    rows_key="nodes",
)
