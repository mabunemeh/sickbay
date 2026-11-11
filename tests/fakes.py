"""A dict-backed fake host for testing capture without root, systemd or Linux."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sickbay.snapshot.source import Kind, RunResult

FIXED_CLOCK = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
JOURNAL_ARGV = ("journalctl", "-o", "json", "--no-pager", "--since=-1h")
UNIT = "ledger\\x2dapi.service"


class FakeSource:
    def __init__(
        self,
        files: dict[str, bytes],
        symlinks: dict[str, str],
        runs: dict[tuple[str, ...], RunResult | Exception],
        table: dict[int, int],
        pid: int,
        names: list[str],
    ) -> None:
        self.files = files
        self.symlinks = symlinks
        self.runs = runs
        self.table = table
        self.pid = pid
        self.names = names
        self.reads: list[str] = []
        self.denied: set[str] = set()

    def _children(self, path: str) -> set[str]:
        prefix = path.rstrip("/") + "/"
        out = set()
        for p in [*self.files, *self.symlinks]:
            if p.startswith(prefix):
                out.add(p[len(prefix) :].split("/", 1)[0])
        return out

    def read_bytes(self, path: str) -> bytes:
        self.reads.append(path)
        if path in self.denied:
            raise PermissionError(13, "Permission denied")
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def listdir(self, path: str) -> list[str]:
        kids = self._children(path)
        if not kids:
            raise FileNotFoundError(path)
        return sorted(kids)

    def kind(self, path: str) -> Kind | None:
        if path in self.files:
            return "file"
        if path in self.symlinks:
            return "symlink"
        return "dir" if self._children(path) else None

    def readlink(self, path: str) -> str:
        return self.symlinks[path]

    def run(self, argv: tuple[str, ...], timeout: float) -> RunResult:
        result = self.runs.get(argv, FileNotFoundError(argv[0]))
        if isinstance(result, Exception):
            raise result
        return result

    def hostnames(self) -> list[str]:
        return self.names

    def process_table(self) -> dict[int, int]:
        return self.table

    def self_pid(self) -> int:
        return self.pid


def journal_line(**fields: str) -> bytes:
    return json.dumps(fields).encode()


def build_source() -> FakeSource:
    """A small host: capturer is pid 101 under sudo (100); pid 200 is the interesting service."""
    files = {
        "/etc/hostname": b"buildhost\n",
        "/etc/machine-id": b"0123456789abcdef0123456789abcdef\n",
        "/etc/passwd": (
            b"root:x:0:0:root:/root:/bin/bash\n"
            b"alice:x:1000:1000:Alice Example,,,:/home/alice:/bin/bash\n"
            b"nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
        ),
        "/etc/resolv.conf": b"nameserver 10.20.30.1\n",
        "/proc/net/fib_trie": (
            b"Main:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 0.0.0.0\n        /0 universe UNICAST\n"
            b"     +-- 10.20.30.0/24 2 0 2\n        |-- 10.20.30.40\n           /32 host LOCAL\n"
            b"     +-- 127.0.0.0/8 2 0 2\n        |-- 127.0.0.1\n           /32 host LOCAL\n"
        ),
        "/proc/meminfo": b"MemTotal:       16000000 kB\nMemFree:         8000000 kB\n",
        "/proc/loadavg": b"0.10 0.20 0.30 1/200 200\n",
        "/proc/sys/fs/file-nr": b"1024\t0\t9223372036854775807\n",
        "/proc/1/cmdline": b"/sbin/init\x00",
        "/proc/1/limits": b"Limit Soft Limit Hard Limit Units\n",
        "/proc/100/cmdline": b"sudo\x00sickbay\x00capture\x00",
        "/proc/101/cmdline": b"python3\x00-m\x00sickbay\x00capture\x00",
        "/proc/101/limits": b"observer limits\n",
        "/proc/102/cmdline": b"child\x00",
        "/proc/2/cmdline": b"",
        "/proc/200/cmdline": b"/usr/bin/python3\x00/srv/ledger/app.py\x00--owner=alice\x00",
        "/proc/200/limits": b"Max open files            512                  512                  files\n",
        "/proc/200/status": b"Name:\tpython3\nUid:\t1000\t1000\t1000\t1000\n",
        "/proc/200/cgroup": b"0::/system.slice/ledger\\x2dapi.service\n",
        "/proc/200/oom_score": b"666\n",
        "/proc/200/environ": b"SECRET_TOKEN=hunter2\x00",
        "/sys/fs/cgroup/cgroup.controllers": b"cpu memory pids\n",
        "/sys/fs/cgroup/cgroup.procs": b"1\n101\n200\n",
        f"/sys/fs/cgroup/system.slice/{UNIT}/memory.max": b"max\n",
        f"/sys/fs/cgroup/system.slice/{UNIT}/pids.current": b"3\n",
        f"/etc/systemd/system/{UNIT}": (
            b'[Service]\nEnvironment=API_KEY=s3cret MODE=prod\nEnvironment="MSG=a b"\n'
            b"ExecStart=/srv/ledger/app.py\nLimitNOFILE=512\n"
        ),
        "/etc/security/limits.d/aux.conf": b"* soft nofile 1024\n",
        "/run/systemd/system/batch.slice": b"[Slice]\nMemoryMax=64M\n",
    }
    symlinks = {
        f"/etc/systemd/system/multi-user.target.wants/{UNIT}": f"/etc/systemd/system/{UNIT}",
        "/etc/os-release": "../usr/lib/os-release",
        "/proc/200/fd/0": "/dev/null",
        "/proc/200/fd/3": "/srv/ledger/spool/a 1.dat",
        "/proc/200/fd/4": "socket:[12345]",
        "/proc/101/fd/0": "/home/alice/capture-notes.txt",
    }
    list_units = (
        "-.slice loaded active active Root Slice\n"
        f"{UNIT} loaded active running Ledger API\n"
        "batch.slice loaded active active Batch Slice\n"
        "cron.timer loaded active waiting Cron\n"
        "ghost.service not-found inactive dead ghost.service\n"
    )
    ps = (
        "    1     0 root     Ss    1000  2000   1       100 /sbin/init\n"
        "  100     1 root     S      500  1000   1        50 sudo sickbay capture\n"
        "  101   100 root     S     9000 20000   1        49 python3 -m sickbay capture\n"
        "  102   101 root     S      100   200   1         1 ps -eo pid,ppid\n"
        "  200     1 alice    Ssl   7000 90000   4        40 /usr/bin/python3 /srv/ledger/app.py\n"
    )
    runs: dict[tuple[str, ...], RunResult | Exception] = {
        ("uname", "-r"): RunResult(0, b"6.18.54.1-microsoft-standard-WSL2\n"),
        ("systemctl", "--version"): RunResult(0, b"systemd 259 (259.2-1)\n+PAM +AUDIT\n"),
        ("systemctl", "list-units", "--all", "--no-pager", "--plain", "--no-legend"): RunResult(
            0, list_units.encode()
        ),
        ("systemctl", "--failed", "--no-pager", "--plain", "--no-legend"): RunResult(0, b""),
        ("ss", "-tulnpH"): RunResult(
            0,
            b'tcp LISTEN 0 128 10.20.30.40:8080 0.0.0.0:* users:(("python3",pid=200,fd=3))\n'
            b"tcp LISTEN 0 128 127.0.0.53%lo:53 0.0.0.0:*\n",
        ),
        ("df", "-P"): RunResult(0, b"Filesystem 1024-blocks Used Available Capacity Mounted on\n"),
        ("df", "-iP"): RunResult(0, b"Filesystem Inodes IUsed IFree IUse% Mounted on\n"),
        ("free", "-b"): RunResult(0, b"              total        used\nMem:    16000000000\n"),
        ("ps", "-eo", "pid,ppid,user,stat,rss,vsz,nlwp,etimes,args", "--no-headers"): RunResult(
            0, ps.encode()
        ),
        JOURNAL_ARGV: RunResult(
            0,
            b"\n".join(
                [
                    journal_line(
                        __CURSOR="s=abc",
                        __REALTIME_TIMESTAMP="1700000002000000",
                        _BOOT_ID="b00t",
                        _HOSTNAME="buildhost",
                        MESSAGE="ledger-api: connect from 10.20.30.99 failed",
                        SYSLOG_IDENTIFIER="ledger-api",
                    ),
                    journal_line(
                        __REALTIME_TIMESTAMP="1700000001000000",
                        MESSAGE="alice : COMMAND=/usr/bin/sickbay capture",
                        SYSLOG_IDENTIFIER="sudo",
                    ),
                    journal_line(
                        __REALTIME_TIMESTAMP="1700000000000000",
                        MESSAGE="started é ok",
                        SYSLOG_IDENTIFIER="systemd",
                    ),
                ]
            )
            + b"\n",
        ),
    }
    show_argv = (
        "systemctl",
        "show",
        "--no-pager",
        "-p",
    )
    from sickbay.snapshot.commands import SHOW_PROPERTIES

    runs[(*show_argv, ",".join(SHOW_PROPERTIES), "--", "-.slice", "batch.slice", UNIT)] = RunResult(
        0,
        f"Id={UNIT}\nEnvironment=API_KEY=s3cret MODE=prod\nMainPID=200\n\nId=batch.slice\n".encode(),
    )
    table = {1: 0, 2: 0, 100: 1, 101: 100, 102: 101, 200: 1}
    return FakeSource(files, symlinks, runs, table, 101, ["buildhost", "buildhost.corp.example"])
