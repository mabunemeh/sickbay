"""The path allowlist. Every path a tool reads is checked here before the HostView is touched.

Path arguments must already be absolute and normalised: anything else is rejected, never
normalised. Matching is per component, so a prefix never matches a sibling such as
"/run/systemd/system.control" against "/run/systemd/system".
"""

from __future__ import annotations

import re
import unicodedata

from sickbay.tools.base import MAX_ARG_BYTES, ToolError, echo

CGROUP_ROOT = "/sys/fs/cgroup"
_PID = "<pid>"
_PID_RE = re.compile(r"[1-9][0-9]{0,9}", re.ASCII)
_UNIT_RE = re.compile(r"[A-Za-z0-9:_.@\\-]+", re.ASCII)
UNIT_PATTERN = r"^[A-Za-z0-9:_.@\\-]+$"
PID_PATTERN = r"^[1-9][0-9]{0,9}$"

# (components, recursive): recursive rules allow the path itself and everything under it.
_RULES: tuple[tuple[tuple[str, ...], bool], ...] = (
    (("proc", "meminfo"), False),
    (("proc", "loadavg"), False),
    (("proc", "uptime"), False),
    (("proc", "pressure"), True),
    (("proc", "sys", "fs"), True),
    *(
        (("proc", _PID, name), False)
        for name in ("limits", "status", "cmdline", "cgroup", "oom_score", "fd")
    ),
    (("sys", "fs", "cgroup"), True),
    (("etc", "systemd"), True),
    (("etc", "security", "limits.conf"), False),
    (("etc", "security", "limits.d"), True),
    (("etc", "os-release"), False),
    (("run", "systemd", "system"), True),
    (("run", "systemd", "transient"), True),
    (("run", "systemd", "system.control"), True),
)


def _bad_text(value: str) -> bool:
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return len(raw) > MAX_ARG_BYTES or any(unicodedata.category(c) == "Cc" for c in value)


def check_path_syntax(path: object, arg: str = "path") -> str:
    """Return path if it is a well-formed absolute normalised host path, else invalid_argument."""
    if not isinstance(path, str):
        raise ToolError("invalid_argument", f"{arg} must be a string")
    if _bad_text(path):
        raise ToolError(
            "invalid_argument",
            f"{arg} must be at most {MAX_ARG_BYTES} bytes of UTF-8 with no control characters",
        )
    if not path.startswith("/"):
        raise ToolError("invalid_argument", f"{arg} must be absolute: {echo(path)}")
    # A backslash is an ordinary filename character (systemd escapes such as "\x2d"); paths
    # are looked up as host paths, never through a shell or Windows API, so it cannot traverse.
    if "%" in path:
        raise ToolError("invalid_argument", f"{arg} must not contain '%': {echo(path)}")
    if path != "/" and any(p in ("", ".", "..") for p in path[1:].split("/")):
        raise ToolError(
            "invalid_argument",
            f"{arg} must be normalised (no '.', '..', '//' or trailing '/'): {echo(path)}",
        )
    return path


def _parts(path: str) -> tuple[str, ...]:
    return () if path == "/" else tuple(path[1:].split("/"))


def _match(part: str, pattern: str) -> bool:
    return _PID_RE.fullmatch(part) is not None if pattern == _PID else part == pattern


def _prefix_match(parts: tuple[str, ...], rule: tuple[str, ...]) -> bool:
    return all(_match(p, r) for p, r in zip(parts, rule, strict=False))


def is_readable(path: str) -> bool:
    parts = _parts(path)
    for rule, recursive in _RULES:
        fits = len(parts) == len(rule) or (recursive and len(parts) > len(rule))
        if fits and _prefix_match(parts, rule):
            return True
    return False


def is_listable(path: str) -> bool:
    """Readable, or an ancestor of something readable (e.g. "/", "/proc", "/proc/1")."""
    if is_readable(path):
        return True
    parts = _parts(path)
    return any(len(parts) < len(rule) and _prefix_match(parts, rule) for rule, _ in _RULES)


def check_readable(path: str) -> str:
    if not is_readable(path):
        raise ToolError("not_allowed", f"path is not on the allowlist: {echo(path)}")
    return path


def check_listable(path: str) -> str:
    if not is_listable(path):
        raise ToolError("not_allowed", f"path is not on the allowlist: {echo(path)}")
    return path


def join(parent: str, name: str) -> str:
    return f"{parent.rstrip('/')}/{name}"


def check_pid(pid: object) -> str:
    if not isinstance(pid, str) or _PID_RE.fullmatch(pid) is None:
        raise ToolError("invalid_argument", f"pid must be decimal digits: {echo(pid)}")
    return pid


def check_unit(unit: object) -> str:
    if (
        not isinstance(unit, str)
        or len(unit) > MAX_ARG_BYTES
        or _UNIT_RE.fullmatch(unit) is None
        or set(unit) == {"."}
    ):
        raise ToolError("invalid_argument", f"not a systemd unit name: {echo(unit)}")
    return unit


def pid_path(pid: object, name: str) -> str:
    return check_readable(f"/proc/{check_pid(pid)}/{name}")


def cgroup_path(path: object) -> str:
    """Host path of a cgroup path argument ("/" is the cgroup root)."""
    cg = check_path_syntax(path, "path")
    return check_readable(CGROUP_ROOT if cg == "/" else CGROUP_ROOT + cg)
