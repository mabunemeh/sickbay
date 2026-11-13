"""list_dir: entries of an allowlisted directory (or an ancestor of one, filtered)."""

from __future__ import annotations

from typing import Any

from sickbay.snapshot.hostview import HostView
from sickbay.tools.base import (
    ROW_LIMIT_DEFAULT,
    Args,
    Result,
    Tool,
    ToolError,
    kind_no_follow,
    limit_property,
    rows_result,
    schema,
    symlink_error,
)
from sickbay.tools.paths import check_listable, check_path_syntax, is_listable, join


def run(view: HostView, args: Args) -> Result:
    path = check_listable(check_path_syntax(args["path"]))
    kind = kind_no_follow(view, path)
    if kind is None:
        raise ToolError("not_found", "no such directory in this snapshot")
    if kind == "symlink":
        raise symlink_error(view, path)
    if kind != "dir":
        raise ToolError("not_a_directory", "path is not a directory; use read_file")
    names = sorted(n for n in view.list_dir(path) if is_listable(join(path, n)))
    result = rows_result("entries", names, args.get("limit", ROW_LIMIT_DEFAULT))
    entries: list[dict[str, Any]] = []
    for name in result["entries"]:
        child = join(path, name)
        entry: dict[str, Any] = {"name": name, "type": view.kind(child) or "other"}
        if entry["type"] == "symlink":
            entry["target"] = view.readlink(child)
        entries.append(entry)
    result["entries"] = entries
    result["path"] = path
    return result


TOOL = Tool(
    name="list_dir",
    description=(
        "List a directory: name, type (file/dir/symlink) and, for a symlink, its raw target. "
        "Works on allowlisted directories and on their parents ('/', '/proc', '/etc', ...), "
        "which show only entries that lead to something readable."
    ),
    input_schema=schema(
        {
            "path": {"type": "string", "description": "Absolute host path of a directory."},
            "limit": limit_property("entries"),
        },
        required=("path",),
    ),
    run=run,
    rows_key="entries",
)
