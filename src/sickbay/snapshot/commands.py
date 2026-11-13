"""The fixed allowlist of commands capture runs."""

from __future__ import annotations

from sickbay.snapshot.hostview import UnknownCommandError

COMMAND_TIMEOUT = 20.0
JOURNAL_TIMEOUT = 60.0
COMMAND_ENV = {"LC_ALL": "C", "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"}

SHOW_PROPERTIES = (
    "Id",
    "Description",
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "MainPID",
    "ExecMainStatus",
    "ExecMainCode",
    "NRestarts",
    "Restart",
    "RestartUSec",
    "StartLimitIntervalUSec",
    "StartLimitBurst",
    "LimitNOFILE",
    "LimitNOFILESoft",
    "MemoryMax",
    "MemoryHigh",
    "MemorySwapMax",
    "MemoryCurrent",
    "MemoryPeak",
    "TasksMax",
    "TasksCurrent",
    "ControlGroup",
    "Slice",
    "FragmentPath",
    "DropInPaths",
    "ExecStart",
    "Environment",
    "User",
    "OOMPolicy",
    "ActiveEnterTimestamp",
    "InactiveExitTimestamp",
    "StateChangeTimestamp",
)

LIST_UNITS_ID = "systemctl-list-units"
SHOW_ID = "systemctl-show"
PS_ID = "ps"

STATIC_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        LIST_UNITS_ID,
        ("systemctl", "list-units", "--all", "--no-pager", "--plain", "--no-legend"),
    ),
    ("systemctl-failed", ("systemctl", "--failed", "--no-pager", "--plain", "--no-legend")),
    ("ss", ("ss", "-tulnpH")),
    ("df", ("df", "-P")),
    ("df-i", ("df", "-iP")),
    ("free", ("free", "-b")),
    (
        PS_ID,
        ("ps", "-eo", "pid,ppid,user,stat,rss,vsz,nlwp,etimes,args", "--no-headers"),
    ),
    ("uname-r", ("uname", "-r")),
    ("systemctl-version", ("systemctl", "--version")),
)

_SHOWN_SUFFIXES = (".service", ".slice", ".scope")


def static_argv(command_id: str) -> tuple[str, ...]:
    for cid, argv in STATIC_COMMANDS:
        if cid == command_id:
            return argv
    raise UnknownCommandError(command_id)


def loaded_units(list_units_stdout: bytes) -> list[str]:
    """Names of loaded services, slices and scopes from `systemctl list-units` output."""
    units: set[str] = set()
    for raw in list_units_stdout.decode("utf-8", "replace").splitlines():
        cols = raw.lstrip(" ●*").split()
        if len(cols) >= 2 and cols[1] == "loaded" and cols[0].endswith(_SHOWN_SUFFIXES):
            units.add(cols[0])
    return sorted(units)


def show_argv(units: list[str]) -> tuple[str, ...]:
    # "--": the root slice is named "-.slice", which would parse as an option.
    return ("systemctl", "show", "--no-pager", "-p", ",".join(SHOW_PROPERTIES), "--", *units)
