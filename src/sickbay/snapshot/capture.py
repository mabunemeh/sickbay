"""Capture a snapshot from a Source. The collection logic is pure; I/O lives in the Source."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sickbay.snapshot.commands import (
    COMMAND_TIMEOUT,
    JOURNAL_TIMEOUT,
    LIST_UNITS_ID,
    PS_ID,
    SHOW_ID,
    STATIC_COMMANDS,
    loaded_units,
    show_argv,
)
from sickbay.snapshot.scrub import Scrubber, gather_facts
from sickbay.snapshot.source import Source
from sickbay.snapshot.writer import SnapshotWriter

PROC_FILES = (
    "/proc/sys/fs/file-nr",
    "/proc/sys/fs/file-max",
    "/proc/sys/fs/nr_open",
    "/proc/meminfo",
    "/proc/loadavg",
    "/proc/uptime",
    "/proc/pressure/cpu",
    "/proc/pressure/memory",
    "/proc/pressure/io",
)
PID_FILES = ("limits", "status", "cmdline", "cgroup", "oom_score")
CGROUP_ROOT = "/sys/fs/cgroup"
CGROUP_FILES = (
    "cgroup.procs",
    "cgroup.controllers",
    "cgroup.subtree_control",
    "cgroup.events",
    "memory.max",
    "memory.high",
    "memory.low",
    "memory.min",
    "memory.current",
    "memory.peak",
    "memory.events",
    "memory.events.local",
    "memory.swap.max",
    "memory.swap.current",
    "pids.max",
    "pids.current",
    "pids.events",
    "cpu.max",
    "cpu.weight",
    "cpu.stat",
    "io.max",
)
ETC_TREES = ("/etc/systemd/system", "/etc/security/limits.d")
RUN_TREES = (
    "/run/systemd/system",
    "/run/systemd/transient",
    "/run/systemd/system.control",
)
ETC_FILES = ("/etc/systemd/system.conf", "/etc/security/limits.conf", "/etc/os-release")
JOURNAL_DROP_FIELDS = ("__CURSOR", "__SEQNUM", "__SEQNUM_ID", "_BOOT_ID", "_MACHINE_ID")
FD_NOTE = "synthetic: lines of '<fd> -> <readlink target>'"
_VANISHED = (FileNotFoundError, ProcessLookupError)


def observer_pids(table: Mapping[int, int], self_pid: int) -> tuple[set[int], set[int]]:
    """Return (excluded, child_roots) for the capturing process.

    excluded: self, all descendants, and ancestors up to but not including PID 1.
    child_roots: self and descendants (a process whose parent is one of these is ours).
    """
    children: dict[int, list[int]] = {}
    for pid, ppid in table.items():
        children.setdefault(ppid, []).append(pid)
    roots = {self_pid}
    stack = [self_pid]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in roots:
                roots.add(child)
                stack.append(child)
    excluded = set(roots)
    pid = table.get(self_pid, 0)
    while pid > 1 and pid not in excluded:
        excluded.add(pid)
        pid = table.get(pid, 0)
    return excluded, roots


def filter_ps(stdout: bytes, excluded: set[int], child_roots: set[int]) -> bytes:
    """Drop ps lines for observer processes (by pid column) and for our own children (by ppid)."""
    kept = []
    for line in stdout.splitlines(keepends=True):
        cols = line.split(None, 2)
        try:
            pid, ppid = int(cols[0]), int(cols[1])
        except (IndexError, ValueError):
            kept.append(line)
            continue
        if pid not in excluded and ppid not in child_roots:
            kept.append(line)
    return b"".join(kept)


def _filter_procs(data: bytes, excluded: set[int]) -> bytes:
    kept = [ln for ln in data.splitlines(keepends=True) if ln.strip() not in _pid_bytes(excluded)]
    return b"".join(kept)


def _pid_bytes(pids: set[int]) -> set[bytes]:
    return {str(p).encode() for p in pids}


class _Capturer:
    def __init__(self, source: Source, writer: SnapshotWriter, scrubber: Scrubber | None) -> None:
        self.src = source
        self.w = writer
        self.scrubber = scrubber
        self.excluded: set[int] = set()
        self.child_roots: set[int] = set()
        self.outputs: dict[str, bytes] = {}

    def s(self, data: bytes) -> bytes:
        return self.scrubber.scrub(data) if self.scrubber else data

    def s_str(self, text: str) -> str:
        return self.scrubber.scrub_str(text) if self.scrubber else text

    # -- filesystem --------------------------------------------------------------------------

    def copy_file(self, path: str, *, quiet_missing: bool = True) -> bytes | None:
        try:
            data = self.src.read_bytes(path)
        except _VANISHED:
            if not quiet_missing:
                self.w.add_error("not found", path=path)
            return None
        except OSError as exc:
            self.w.add_error(self.s_str(exc.strerror or type(exc).__name__), path=path)
            return None
        if path.endswith("/cgroup.procs"):
            data = _filter_procs(data, self.excluded)
        self.w.add_file(path, self.s(data))
        return data

    def copy_tree(self, root: str) -> None:
        try:
            names = self.src.listdir(root)
        except _VANISHED:
            return
        except OSError as exc:
            self.w.add_error(self.s_str(exc.strerror or type(exc).__name__), path=root)
            return
        for name in names:
            path = f"{root}/{name}"
            kind = self.src.kind(path)
            if kind == "dir":
                self.copy_tree(path)
            elif kind == "file":
                self.copy_file(path)
            elif kind == "symlink":
                try:
                    self.w.add_symlink(path, self.s_str(self.src.readlink(path)))
                except OSError as exc:
                    self.w.add_error(self.s_str(exc.strerror or "readlink failed"), path=path)

    def walk_cgroups(self, path: str) -> None:
        for name in CGROUP_FILES:
            if self.src.kind(f"{path}/{name}") == "file":
                self.copy_file(f"{path}/{name}")
        try:
            names = self.src.listdir(path)
        except OSError as exc:
            self.w.add_error(self.s_str(exc.strerror or "listdir failed"), path=path)
            return
        for name in names:
            if self.src.kind(f"{path}/{name}") == "dir":
                self.walk_cgroups(f"{path}/{name}")

    def fd_listing(self, pid: int) -> bytes | None:
        base = f"/proc/{pid}/fd"
        try:
            fds = sorted((n for n in self.src.listdir(base) if n.isdigit()), key=int)
        except _VANISHED:
            return None
        except OSError as exc:
            self.w.add_error(self.s_str(exc.strerror or "listdir failed"), path=base)
            return None
        lines = []
        for fd in fds:
            try:
                lines.append(f"{fd} -> {self.src.readlink(f'{base}/{fd}')}\n")
            except OSError:
                continue
        return "".join(lines).encode("utf-8", "surrogateescape")

    def capture_proc(self) -> None:
        for path in PROC_FILES:
            self.copy_file(path)
        pids = sorted(int(n) for n in self.src.listdir("/proc") if n.isdigit())
        for pid in pids:
            if pid in self.excluded:
                continue
            try:
                cmdline = self.src.read_bytes(f"/proc/{pid}/cmdline")
            except _VANISHED:
                continue
            except OSError as exc:
                self.w.add_error(
                    self.s_str(exc.strerror or "read failed"), path=f"/proc/{pid}/cmdline"
                )
                continue
            if not cmdline:
                continue  # kernel thread (or zombie): no cmdline
            self.w.add_file(f"/proc/{pid}/cmdline", self.s(cmdline))
            for name in PID_FILES:
                if name != "cmdline":
                    self.copy_file(f"/proc/{pid}/{name}")
            listing = self.fd_listing(pid)
            if listing is not None:
                self.w.add_file(f"/proc/{pid}/fd", self.s(listing), note=FD_NOTE)

    # -- commands and journal ------------------------------------------------------------------

    def run_command(self, command_id: str, argv: tuple[str, ...]) -> bytes | None:
        try:
            res = self.src.run(argv, COMMAND_TIMEOUT)
        except (OSError, TimeoutError) as exc:
            self.w.add_error(self.s_str(str(exc)), ident=command_id)
            return None
        stdout = res.stdout
        if command_id == PS_ID:
            stdout = filter_ps(stdout, self.excluded, self.child_roots)
        stdout = self.s(stdout)
        self.w.add_command(command_id, [self.s_str(a) for a in argv], res.exit_code, stdout)
        self.outputs[command_id] = stdout
        return stdout

    def capture_commands(self) -> None:
        listing: bytes | None = None
        for command_id, argv in STATIC_COMMANDS:
            out = self.run_command(command_id, argv)
            if command_id == LIST_UNITS_ID:
                listing = out
        units = loaded_units(listing) if listing else []
        if units:
            self.run_command(SHOW_ID, show_argv(units))
        else:
            self.w.add_error("no loaded units to show", ident=SHOW_ID)

    def capture_journal(self, since: str) -> None:
        argv = ("journalctl", "-o", "json", "--no-pager", f"--since={since}")
        try:
            res = self.src.run(argv, JOURNAL_TIMEOUT)
        except (OSError, TimeoutError) as exc:
            self.w.add_error(self.s_str(str(exc)), ident="journal")
            self.w.write_journal([], since)
            return
        entries: list[tuple[int, bytes]] = []
        for raw in res.stdout.splitlines():
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                self.w.add_error("unparseable journal line skipped", ident="journal")
                continue
            if not isinstance(obj, dict) or obj.get("SYSLOG_IDENTIFIER") == "sudo":
                continue
            for field in JOURNAL_DROP_FIELDS:
                obj.pop(field, None)
            obj = self.scrub_json(obj)
            ts = obj.get("__REALTIME_TIMESTAMP", "0")
            line = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            entries.append((int(ts) if str(ts).isdigit() else 0, line.encode("utf-8")))
        entries.sort()
        self.w.write_journal([line for _, line in entries], since)

    def scrub_json(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.s_str(value)
        if isinstance(value, list):
            return [self.scrub_json(v) for v in value]
        if isinstance(value, dict):
            return {k: self.scrub_json(v) for k, v in value.items()}
        return value


def _now() -> datetime:
    return datetime.now(UTC)


def capture(
    source: Source,
    dest: Path,
    *,
    since: str = "-1h",
    scrub: bool = True,
    clock: Callable[[], datetime] = _now,
) -> dict[str, Any]:
    """Capture `source` into `dest` and return the manifest."""
    scrubber = Scrubber(gather_facts(source)) if scrub else None
    writer = SnapshotWriter(dest)
    cap = _Capturer(source, writer, scrubber)
    cap.excluded, cap.child_roots = observer_pids(source.process_table(), source.self_pid())

    cap.capture_proc()
    cap.walk_cgroups(CGROUP_ROOT)
    for tree in (*ETC_TREES, *RUN_TREES):
        cap.copy_tree(tree)
    for path in ETC_FILES:
        kind = source.kind(path)
        if kind == "file":
            cap.copy_file(path)
        elif kind == "symlink":
            try:
                writer.add_symlink(path, cap.s_str(source.readlink(path)))
            except OSError as exc:
                writer.add_error(cap.s_str(exc.strerror or "readlink failed"), path=path)
    cap.capture_commands()
    cap.capture_journal(since)

    def first_line(command_id: str) -> str:
        text = cap.outputs.get(command_id, b"").decode("utf-8", "replace")
        return (text.splitlines() or ["unknown"])[0].strip() or "unknown"

    return writer.finish(
        captured_at=clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        scrubbed=scrub,
        host={"kernel": first_line("uname-r"), "systemd": first_line("systemctl-version")},
        excluded_pids_count=len(cap.excluded),
        scrub_counts=dict(scrubber.counts) if scrubber else {},
    )
