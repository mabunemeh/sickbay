"""read_file: a page of an allowlisted file. Symlinks are reported, never followed."""

from __future__ import annotations

from sickbay.snapshot.hostview import HostView
from sickbay.tools.base import Args, Result, Tool, ToolError, decode, read_regular, schema
from sickbay.tools.paths import check_path_syntax, check_readable

MAX_BYTES_DEFAULT = 4096
MAX_BYTES_MIN = 4  # one whole UTF-8 character always fits, so a page always makes progress
MAX_BYTES_MAX = 6000
MAX_OFFSET = 1 << 24


def _sequence_length(lead: int) -> int:
    if 0xC2 <= lead <= 0xDF:
        return 2
    if 0xE0 <= lead <= 0xEF:
        return 3
    if 0xF0 <= lead <= 0xF4:
        return 4
    return 1


def page(data: bytes, offset: int, max_bytes: int) -> bytes:
    """data[offset:offset+max_bytes], ending before a valid UTF-8 sequence the cut would split.

    Invalid bytes are not moved over; they decode as U+FFFD but still count as consumed, so
    concatenating pages (offset += len(page)) gives back the original bytes exactly.
    """
    end = min(len(data), offset + max_bytes)
    for start in range(end - 1, max(offset, end - 4) - 1, -1):
        if data[start] & 0xC0 == 0x80:
            continue  # continuation byte: keep looking for its lead
        n = _sequence_length(data[start])
        if start + n > end and start > offset:
            try:
                data[start : start + n].decode("utf-8")
            except UnicodeDecodeError:
                break
            end = start
        break
    return data[offset:end]


def run(view: HostView, args: Args) -> Result:
    path = check_readable(check_path_syntax(args["path"]))
    data = read_regular(view, path)
    if data is None:
        raise ToolError("not_found", "no such file in this snapshot")
    offset = args.get("offset", 0)
    if offset > len(data):
        raise ToolError(
            "invalid_argument", f"offset {offset} is beyond the end ({len(data)} bytes)"
        )
    chunk = page(data, offset, args.get("max_bytes", MAX_BYTES_DEFAULT))
    return {
        "path": path,
        "size": len(data),
        "offset": offset,
        "returned": len(chunk),
        "content": decode(chunk.replace(b"\x00", b" ")),
        "truncated": offset + len(chunk) < len(data),
    }


TOOL = Tool(
    name="read_file",
    description=(
        "Read an allowlisted file: /proc/{meminfo,loadavg,uptime}, /proc/pressure/*, "
        "/proc/sys/fs/*, /proc/<pid>/{limits,status,cmdline,cgroup,oom_score,fd}, "
        "/sys/fs/cgroup/*, /etc/systemd/*, /etc/security/limits.conf and limits.d/*, "
        "/etc/os-release, /run/systemd/{system,transient,system.control}/*. Paths must be "
        "absolute and normalised. Use offset to page through large files. Symlinks are "
        "reported with their target, never followed."
    ),
    input_schema=schema(
        {
            "path": {"type": "string", "description": "Absolute host path."},
            "offset": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_OFFSET,
                "description": "Byte offset to start at (default 0).",
            },
            "max_bytes": {
                "type": "integer",
                "minimum": MAX_BYTES_MIN,
                "maximum": MAX_BYTES_MAX,
                "description": f"Bytes to return (default {MAX_BYTES_DEFAULT}).",
            },
        },
        required=("path",),
    ),
    run=run,
    raw_keys=frozenset({"content"}),
)
