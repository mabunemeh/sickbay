"""Where capture reads from: the Source protocol and the live Linux implementation."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sickbay.snapshot.commands import (
    COMMAND_ENV,
    COMMAND_TIMEOUT,
    JOURNAL_TIMEOUT,
    LIST_UNITS_ID,
    SHOW_ID,
    loaded_units,
    show_argv,
    static_argv,
)
from sickbay.snapshot.hostview import CommandResult, HostView

Kind = Literal["file", "dir", "symlink", "other"]


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    stdout: bytes


class Source(Protocol):
    """Raw access to a host, used by capture. Errors are OSError subclasses."""

    def read_bytes(self, path: str) -> bytes: ...

    def listdir(self, path: str) -> list[str]: ...

    def kind(self, path: str) -> Kind | None: ...

    def readlink(self, path: str) -> str: ...

    def run(self, argv: tuple[str, ...], timeout: float) -> RunResult: ...

    def hostnames(self) -> list[str]: ...

    def process_table(self) -> dict[int, int]:
        """pid -> ppid for every process."""
        ...

    def self_pid(self) -> int: ...


def parse_stat_ppid(stat: bytes) -> int:
    """ppid from /proc/<pid>/stat (the comm field may contain spaces and parentheses)."""
    return int(stat.rsplit(b")", 1)[1].split()[1])


class LiveSource:
    """The running Linux host. Also implements HostView for batch 2 live mode."""

    def __init__(self, since: str = "-1h") -> None:
        self.since = since

    def read_bytes(self, path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    def listdir(self, path: str) -> list[str]:
        return sorted(os.listdir(path))

    def kind(self, path: str) -> Kind | None:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None
        import stat

        if stat.S_ISLNK(st.st_mode):
            return "symlink"
        if stat.S_ISDIR(st.st_mode):
            return "dir"
        return "file" if stat.S_ISREG(st.st_mode) else "other"

    def readlink(self, path: str) -> str:
        return os.readlink(path)

    def run(self, argv: tuple[str, ...], timeout: float) -> RunResult:
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                timeout=timeout,
                env={**os.environ, **COMMAND_ENV},
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"timed out after {timeout:g}s") from exc
        return RunResult(proc.returncode, proc.stdout)

    def hostnames(self) -> list[str]:
        names = [socket.gethostname()]
        with contextlib.suppress(OSError):
            names.append(socket.getfqdn())
        return names

    def process_table(self) -> dict[int, int]:
        table: dict[int, int] = {}
        for name in self.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                table[int(name)] = parse_stat_ppid(self.read_bytes(f"/proc/{name}/stat"))
            except (OSError, IndexError, ValueError):
                continue
        return table

    def self_pid(self) -> int:
        return os.getpid()

    # -- HostView ---------------------------------------------------------------------------

    def read_file(self, path: str) -> bytes:
        return self.read_bytes(path)

    def list_dir(self, path: str) -> list[str]:
        return self.listdir(path)

    def command(self, command_id: str) -> CommandResult:
        if command_id == SHOW_ID:
            listing = self.run(static_argv(LIST_UNITS_ID), COMMAND_TIMEOUT)
            argv = show_argv(loaded_units(listing.stdout))
        else:
            argv = static_argv(command_id)
        res = self.run(argv, COMMAND_TIMEOUT)
        return CommandResult(argv, res.exit_code, res.stdout)

    def journal(self) -> Iterator[dict[str, Any]]:
        argv = ("journalctl", "-o", "json", "--no-pager", f"--since={self.since}")
        for line in self.run(argv, JOURNAL_TIMEOUT).stdout.splitlines():
            if line.strip():
                yield json.loads(line)


def _check_live_is_hostview(src: LiveSource) -> HostView:
    return src
