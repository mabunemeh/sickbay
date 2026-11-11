import json
from pathlib import Path
from typing import Any

from fakes import FIXED_CLOCK, UNIT, build_source

from sickbay.snapshot import Snapshot, capture
from sickbay.snapshot.capture import filter_ps, observer_pids
from sickbay.snapshot.source import RunResult

FIXTURE = Path(__file__).parent / "fixtures" / "basic"


def run_capture(tmp_path: Path, scrub: bool = True) -> tuple[Snapshot, dict[str, Any]]:
    manifest = capture(build_source(), tmp_path / "snap", scrub=scrub, clock=lambda: FIXED_CLOCK)
    return Snapshot.open(tmp_path / "snap"), manifest


def test_observer_pids_self_descendants_and_ancestors() -> None:
    table = {1: 0, 100: 1, 101: 100, 102: 101, 103: 102, 200: 1, 201: 100}
    excluded, roots = observer_pids(table, 101)
    assert excluded == {100, 101, 102, 103}
    assert roots == {101, 102, 103}


def test_observer_never_excludes_pid_1() -> None:
    excluded, _ = observer_pids({1: 0, 50: 1}, 50)
    assert excluded == {50}


def test_filter_ps_by_pid_and_child_ppid() -> None:
    out = b"  1  0 a\n 100  1 sudo\n 101 100 py\n 102 101 ps\n 200  1 svc\n"
    assert filter_ps(out, {100, 101, 102}, {101, 102}) == b"  1  0 a\n 200  1 svc\n"


def test_capture_excludes_observer_and_kernel_threads(tmp_path: Path) -> None:
    snap, manifest = run_capture(tmp_path)
    paths = {f["path"] for f in manifest["files"]}
    assert not any(p.startswith(("/proc/100/", "/proc/101/", "/proc/102/")) for p in paths)
    assert not any(p.startswith("/proc/2/") for p in paths)
    assert "/proc/200/limits" in paths
    assert manifest["excluded_pids_count"] == 3
    assert snap.read_file("/sys/fs/cgroup/cgroup.procs") == b"1\n200\n"
    ps = snap.command("ps").stdout.decode()
    assert "sickbay" not in ps and " ps " not in ps and "200" in ps


def test_capture_never_reads_environ(tmp_path: Path) -> None:
    src = build_source()
    capture(src, tmp_path / "snap")
    assert not any(p.endswith("/environ") for p in src.reads)
    stored = b"".join(p.read_bytes() for p in (tmp_path / "snap").rglob("*") if p.is_file())
    assert b"hunter2" not in stored


def test_passwd_never_stored(tmp_path: Path) -> None:
    _, manifest = run_capture(tmp_path)
    assert not any(f["path"] == "/etc/passwd" for f in manifest["files"])


def test_nul_cmdline_symlink_and_fd_listing(tmp_path: Path) -> None:
    snap, manifest = run_capture(tmp_path)
    assert snap.read_file("/proc/200/cmdline") == (
        b"/usr/bin/python3\x00/srv/ledger/app.py\x00--owner=user1000\x00"
    )
    link = f"/etc/systemd/system/multi-user.target.wants/{UNIT}"
    assert snap.readlink(link) == f"/etc/systemd/system/{UNIT}"
    assert snap.readlink("/etc/os-release") == "../usr/lib/os-release"
    fd = snap.read_file("/proc/200/fd").decode()
    assert fd == "0 -> /dev/null\n3 -> /srv/ledger/spool/a 1.dat\n4 -> socket:[12345]\n"
    entry = next(f for f in manifest["files"] if f["path"] == "/proc/200/fd")
    assert "synthetic" in entry["note"]


def test_journal_drops_sudo_and_fields_and_sorts(tmp_path: Path) -> None:
    snap, manifest = run_capture(tmp_path)
    entries = list(snap.journal())
    assert [e["SYSLOG_IDENTIFIER"] for e in entries] == ["systemd", "ledger-api"]
    assert all("__CURSOR" not in e and "_BOOT_ID" not in e for e in entries)
    assert entries[1]["_HOSTNAME"] == "host"
    assert manifest["journal"]["entries"] == 2
    raw = (tmp_path / "snap" / "journal.jsonl").read_text(encoding="utf-8")
    assert "é" in raw and ": " not in raw.splitlines()[0]


def test_systemctl_show_only_loaded_services_slices_scopes(tmp_path: Path) -> None:
    snap, _ = run_capture(tmp_path)
    argv = snap.command("systemctl-show").argv
    assert argv[-4:] == ("--", "-.slice", "batch.slice", UNIT)
    assert "cron.timer" not in argv and "ghost.service" not in argv
    assert b"s3cret" not in snap.command("systemctl-show").stdout


def test_host_and_scrub_counts(tmp_path: Path) -> None:
    _, manifest = run_capture(tmp_path)
    assert manifest["host"] == {
        "kernel": "6.18.54.1-microsoft-standard-WSL2",
        "systemd": "systemd 259 (259.2-1)",
    }
    assert manifest["scrubbed"] is True
    assert manifest["scrub_counts"]["username"] > 0
    assert manifest["captured_at"] == "2026-01-02T03:04:05Z"


def test_no_scrub_marks_manifest_and_keeps_values(tmp_path: Path) -> None:
    snap, manifest = run_capture(tmp_path, scrub=False)
    assert manifest["scrubbed"] is False and manifest["scrub_counts"] == {}
    assert b"alice" in snap.read_file("/proc/200/cmdline")


def test_errors_recorded_not_raised(tmp_path: Path) -> None:
    src = build_source()
    src.denied.add("/proc/200/limits")
    src.runs[("df", "-P")] = TimeoutError("timed out after 20s")
    del src.runs[("free", "-b")]
    manifest = capture(src, tmp_path / "snap", clock=lambda: FIXED_CLOCK)
    errors = {e.get("path") or e.get("id"): e["error"] for e in manifest["errors"]}
    assert errors["/proc/200/limits"] == "Permission denied"
    assert errors["df"].startswith("timed out")
    assert "free" in errors
    assert Snapshot.open(tmp_path / "snap").verify() == []


def test_failed_command_exit_code_is_kept(tmp_path: Path) -> None:
    src = build_source()
    key = ("systemctl", "--failed", "--no-pager", "--plain", "--no-legend")
    src.runs[key] = RunResult(1, b"x\n")
    capture(src, tmp_path / "snap")
    assert Snapshot.open(tmp_path / "snap").command("systemctl-failed").exit_code == 1


def test_capture_is_deterministic(tmp_path: Path) -> None:
    capture(build_source(), tmp_path / "a", clock=lambda: FIXED_CLOCK)
    capture(build_source(), tmp_path / "b", clock=lambda: FIXED_CLOCK)
    for p in (tmp_path / "a").rglob("*"):
        if p.is_file():
            assert p.read_bytes() == (tmp_path / "b" / p.relative_to(tmp_path / "a")).read_bytes()


def test_manifest_format_is_sorted_lf(tmp_path: Path) -> None:
    run_capture(tmp_path)
    raw = (tmp_path / "snap" / "manifest.json").read_bytes()
    assert b"\r" not in raw and raw.endswith(b"}\n")
    data = json.loads(raw)
    assert raw == (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()
    assert [f["path"] for f in data["files"]] == sorted(f["path"] for f in data["files"])


def test_committed_fixture_matches_generator(tmp_path: Path) -> None:
    capture(build_source(), tmp_path / "fresh", clock=lambda: FIXED_CLOCK)
    fresh = {
        p.relative_to(tmp_path / "fresh").as_posix(): p.read_bytes()
        for p in (tmp_path / "fresh").rglob("*")
        if p.is_file()
    }
    committed = {
        p.relative_to(FIXTURE).as_posix(): p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()
    }
    assert fresh == committed
