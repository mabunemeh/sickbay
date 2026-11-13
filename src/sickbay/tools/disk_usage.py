"""disk_usage: `df -P` and `df -iP` rows joined by mount point, columns verbatim."""

from __future__ import annotations

from sickbay.snapshot.hostview import HostView
from sickbay.tools.base import Args, Result, Tool, ToolError, command_text, rows_result, schema

BLOCK_COLUMNS = ("1024-blocks", "used", "available", "capacity")
INODE_COLUMNS = ("inodes", "iused", "ifree", "iuse%")


def parse_df(text: str, columns: tuple[str, ...]) -> list[dict[str, str]]:
    rows = []
    for line in text.splitlines():
        if line.startswith("Filesystem"):
            continue
        cols = line.split(None, 5)
        if len(cols) < 6:
            continue
        rows.append(
            {
                "filesystem": cols[0],
                **dict(zip(columns, cols[1:5], strict=True)),
                "mounted_on": cols[5],
            }
        )
    return rows


def _optional(view: HostView, command_id: str) -> str | None:
    try:
        return command_text(view, command_id)
    except ToolError:
        return None


def run(view: HostView, args: Args) -> Result:
    blocks_text, inodes_text = _optional(view, "df"), _optional(view, "df-i")
    if blocks_text is None and inodes_text is None:
        raise ToolError("unavailable", "this snapshot has no 'df' or 'df-i' output")
    blocks = parse_df(blocks_text or "", BLOCK_COLUMNS)
    inodes = parse_df(inodes_text or "", INODE_COLUMNS)
    rows = []
    for row in blocks:
        match = next((r for r in inodes if r["mounted_on"] == row["mounted_on"]), None)
        if match is not None:
            inodes.remove(match)
            row = {**match, **row}
        rows.append(row)
    rows += inodes
    return rows_result("mounts", rows, len(rows))


TOOL = Tool(
    name="disk_usage",
    description=(
        "Disk space and inode usage per mount (df -P and df -iP joined by mount point; "
        "1024-blocks, used and available are KiB)."
    ),
    input_schema=schema({}),
    run=run,
    rows_key="mounts",
)
