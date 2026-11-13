import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from sickbay.snapshot import Snapshot, SnapshotError, UnsupportedVersionError
from sickbay.snapshot.pathenc import InvalidHostPathError
from sickbay.snapshot.writer import sha256_hex

FIXTURE = Path(__file__).parent / "fixtures" / "basic"
UNIT = "ledger\\x2dapi.service"


@pytest.fixture
def snap() -> Snapshot:
    return Snapshot.open(FIXTURE)


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    dest = tmp_path / "copy"
    shutil.copytree(FIXTURE, dest)
    return dest


def edit_manifest(root: Path, change: object) -> None:
    path = root / "manifest.json"
    data = json.loads(path.read_text())
    change(data)  # type: ignore[operator]
    path.write_text(json.dumps(data))


def test_fixture_verifies_and_is_scrubbed(snap: Snapshot) -> None:
    assert snap.verify() == []
    assert snap.manifest["scrubbed"] is True


def test_read_list_readlink(snap: Snapshot) -> None:
    assert b"LimitNOFILE=512" in snap.read_file(f"/etc/systemd/system/{UNIT}")
    assert snap.read_file("/etc/security/limits.d/aux.conf") == b"* soft nofile 1024\n"
    assert snap.list_dir("/etc/systemd/system") == [UNIT, "multi-user.target.wants"]
    assert "200" in snap.list_dir("/proc")
    assert snap.list_dir("/etc/systemd/system/multi-user.target.wants") == [UNIT]
    assert snap.readlink("/etc/os-release") == "../usr/lib/os-release"
    assert snap.command("uname-r").stdout == b"6.18.54.1-microsoft-standard-WSL2\n"


def test_missing_and_wrong_kind(snap: Snapshot) -> None:
    with pytest.raises(FileNotFoundError):
        snap.read_file("/etc/shadow")
    with pytest.raises(IsADirectoryError):
        snap.read_file("/etc")
    with pytest.raises(NotADirectoryError):
        snap.list_dir("/etc/os-release")
    with pytest.raises(OSError, match="symlink"):
        snap.read_file("/etc/os-release")
    with pytest.raises(KeyError):
        snap.command("nope")


@pytest.mark.parametrize("bad", ["etc/os-release", "/etc/../etc/os-release", "/etc//x", "/etc/"])
def test_path_guard(snap: Snapshot, bad: str) -> None:
    for call in (snap.read_file, snap.list_dir, snap.readlink):
        with pytest.raises(InvalidHostPathError):
            call(bad)


def test_round_trip_byte_identical(snap: Snapshot, tmp_path: Path) -> None:
    snap.write(tmp_path / "out")
    src = {p.relative_to(FIXTURE): p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}
    out = {
        p.relative_to(tmp_path / "out"): p.read_bytes()
        for p in (tmp_path / "out").rglob("*")
        if p.is_file()
    }
    assert src == out
    assert Snapshot.open(tmp_path / "out").verify() == []


def test_write_refuses_non_empty_dest(snap: Snapshot, tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x").write_text("x")
    with pytest.raises(FileExistsError):
        snap.write(tmp_path / "d")


def test_verify_catches_flipped_byte(copy: Path) -> None:
    target = copy / "fs" / "proc" / "200" / "limits"
    data = bytearray(target.read_bytes())
    data[0] ^= 0x01
    target.write_bytes(bytes(data))
    problems = Snapshot.open(copy).verify()
    assert len(problems) == 1 and "sha256 mismatch" in problems[0]


def test_verify_catches_size_change(copy: Path) -> None:
    target = copy / "commands" / "df.txt"
    target.write_bytes(target.read_bytes() + b"x")
    assert any("size" in p for p in Snapshot.open(copy).verify())


def test_verify_catches_extra_and_missing_file(copy: Path) -> None:
    (copy / "fs" / "etc" / "stray").write_bytes(b"x")
    (copy / "fs" / "proc" / "loadavg").unlink()
    problems = Snapshot.open(copy).verify()
    assert any("unexpected file" in p and "stray" in p for p in problems)
    assert any("missing" in p and "loadavg" in p for p in problems)


def test_verify_catches_journal_tamper(copy: Path) -> None:
    (copy / "journal.jsonl").write_bytes(b'{"a":1}\n')
    assert any(p.startswith("journal") for p in Snapshot.open(copy).verify())


def test_rejects_unknown_version(copy: Path) -> None:
    edit_manifest(copy, lambda m: m.update(format_version=2))
    with pytest.raises(UnsupportedVersionError):
        Snapshot.open(copy)


def test_rejects_foreign_and_unreadable(copy: Path, tmp_path: Path) -> None:
    (copy / "manifest.json").write_text('{"format": "other"}')
    with pytest.raises(UnsupportedVersionError):
        Snapshot.open(copy)
    with pytest.raises(SnapshotError):
        Snapshot.open(tmp_path / "missing")
    (copy / "manifest.json").write_text("not json")
    with pytest.raises(SnapshotError):
        Snapshot.open(copy)


def test_rejects_traversal_in_manifest(copy: Path) -> None:
    edit_manifest(copy, lambda m: m["files"][0].update(disk="../../outside"))
    with pytest.raises(SnapshotError, match="unsafe"):
        Snapshot.open(copy)


def test_rejects_malformed_manifest(copy: Path) -> None:
    edit_manifest(copy, lambda m: m.pop("files"))
    with pytest.raises(SnapshotError, match="malformed"):
        Snapshot.open(copy)


def test_verify_catches_directory_at_file_path(copy: Path) -> None:
    target = copy / "fs" / "proc" / "loadavg"
    target.unlink()
    target.mkdir()
    problems = Snapshot.open(copy).verify()
    assert any("not a regular file" in p and "loadavg" in p for p in problems)


def test_verify_catches_symlink_at_file_path(copy: Path) -> None:
    target = copy / "fs" / "proc" / "loadavg"
    data = target.read_bytes()
    target.unlink()
    (copy / "real").write_bytes(data)
    try:
        target.symlink_to(copy / "real")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available here")
    problems = Snapshot.open(copy).verify()
    assert any("not a regular file" in p and "loadavg" in p for p in problems)


def test_verify_catches_journal_entry_count_mismatch(copy: Path) -> None:
    edit_manifest(copy, lambda m: m["journal"].update(entries=99))
    problems = Snapshot.open(copy).verify()
    assert problems == ["journal: entry count mismatch"]


# -- hardened reads (batch 2): every read checks size and sha256 and refuses links -------------


def test_read_fails_closed_on_flipped_byte(copy: Path) -> None:
    target = copy / "fs" / "proc" / "200" / "limits"
    data = bytearray(target.read_bytes())
    data[-2] ^= 0x01
    target.write_bytes(bytes(data))
    snap = Snapshot.open(copy)
    with pytest.raises(SnapshotError, match="sha256"):
        snap.read_file("/proc/200/limits")
    assert snap.read_file("/proc/loadavg")  # other files still read


def test_read_fails_closed_on_size_change(copy: Path) -> None:
    target = copy / "commands" / "ps.txt"
    target.write_bytes(target.read_bytes() + b"x")
    with pytest.raises(SnapshotError, match="size"):
        Snapshot.open(copy).command("ps")


def test_read_fails_closed_on_tampered_manifest_size(copy: Path) -> None:
    edit_manifest(copy, lambda m: m["files"][0].update(size=m["files"][0]["size"] + 1))
    path = json.loads((copy / "manifest.json").read_text())["files"][0]["path"]
    with pytest.raises(SnapshotError, match="size"):
        Snapshot.open(copy).read_file(path)


def test_read_fails_closed_on_missing_disk_file(copy: Path) -> None:
    (copy / "fs" / "proc" / "loadavg").unlink()
    with pytest.raises(SnapshotError):
        Snapshot.open(copy).read_file("/proc/loadavg")


def test_read_fails_closed_on_directory_at_file_path(copy: Path) -> None:
    target = copy / "fs" / "proc" / "loadavg"
    target.unlink()
    target.mkdir()
    with pytest.raises(SnapshotError):
        Snapshot.open(copy).read_file("/proc/loadavg")


def test_journal_fails_closed_on_tamper_and_count(copy: Path) -> None:
    edit_manifest(copy, lambda m: m["journal"].update(entries=99))
    with pytest.raises(SnapshotError, match="entry count"):
        list(Snapshot.open(copy).journal())
    (copy / "journal.jsonl").write_bytes(b'{"a":1}\n')
    with pytest.raises(SnapshotError, match="sha256"):
        list(Snapshot.open(copy).journal())


def test_journal_rejects_unparseable_line_even_with_matching_hash(copy: Path) -> None:
    data = b"not json\n[1]\n"
    (copy / "journal.jsonl").write_bytes(data)
    edit_manifest(copy, lambda m: m["journal"].update(entries=2, sha256=sha256_hex(data)))
    with pytest.raises(SnapshotError, match="unparseable"):
        list(Snapshot.open(copy).journal())


def test_manifest_symlink_overlapping_a_directory_is_rejected(copy: Path) -> None:
    edit_manifest(copy, lambda m: m["symlinks"].append({"path": "/proc/200", "target": "/etc"}))
    with pytest.raises(SnapshotError, match="symlink"):
        Snapshot.open(copy)


def test_manifest_file_overlapping_a_symlink_is_rejected(copy: Path) -> None:
    edit_manifest(copy, lambda m: m["symlinks"].append({"path": "/proc/loadavg", "target": "/etc"}))
    with pytest.raises(SnapshotError, match="twice"):
        Snapshot.open(copy)


def test_kind(snap: Snapshot) -> None:
    assert snap.kind("/") == "dir"
    assert snap.kind("/proc/200") == "dir"
    assert snap.kind("/proc/200/fd") == "file"
    assert snap.kind("/etc/os-release") == "symlink"
    assert snap.kind("/nope") is None
    with pytest.raises(InvalidHostPathError):
        snap.kind("/etc/")


# -- review round -------------------------------------------------------------------------------


def _link_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks here: {exc}")


@pytest.mark.parametrize("inside", [True, False])
def test_symlinked_manifest_is_rejected(copy: Path, tmp_path: Path, inside: bool) -> None:
    manifest = copy / "manifest.json"
    real = (copy / "fs" / "real-manifest") if inside else (tmp_path / "outside-manifest")
    real.write_bytes(manifest.read_bytes())
    manifest.unlink()
    _link_or_skip(manifest, real)
    with pytest.raises(SnapshotError):
        Snapshot.open(copy)


def test_os_symlink_to_identical_file_inside_root_is_rejected(copy: Path) -> None:
    """Hash and stays-under-root both pass here; only the per-component link check fails it."""
    target = copy / "fs" / "proc" / "200" / "limits"
    twin = copy / "fs" / "proc" / "200" / "limits-twin"
    twin.write_bytes(target.read_bytes())
    target.unlink()
    _link_or_skip(target, twin)
    with pytest.raises(SnapshotError, match="not a plain"):
        Snapshot.open(copy).read_file("/proc/200/limits")


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="no O_NOFOLLOW on this platform")
def test_open_does_not_follow_a_link_swapped_in_after_the_check(
    copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the race: lstat sees a regular file, open meets a link (O_NOFOLLOW)."""
    target = copy / "fs" / "proc" / "200" / "limits"
    twin = copy / "fs" / "proc" / "200" / "limits-twin"
    twin.write_bytes(target.read_bytes())
    target.unlink()
    _link_or_skip(target, twin)
    snap = Snapshot.open(copy)
    monkeypatch.setattr(os, "lstat", os.stat)
    with pytest.raises(SnapshotError, match="cannot read"):
        snap.read_file("/proc/200/limits")


def test_journal_line_that_is_not_an_object_fails_closed(copy: Path) -> None:
    data = b"[1]\n"
    (copy / "journal.jsonl").write_bytes(data)
    edit_manifest(copy, lambda m: m["journal"].update(entries=1, sha256=sha256_hex(data)))
    with pytest.raises(SnapshotError, match="not a JSON object"):
        list(Snapshot.open(copy).journal())


def _cmd(m: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in m["commands"] if c["id"] == "ps")  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m["journal"].pop("entries"),
        lambda m: m["journal"].update(entries="2"),
        lambda m: m["journal"].update(entries=True),
        lambda m: m["journal"].update(entries=-1),
        lambda m: m["journal"].pop("sha256"),
        lambda m: _cmd(m).pop("argv"),
        lambda m: _cmd(m).update(argv="ps -e"),
        lambda m: _cmd(m).update(argv=["ps", 1]),
        lambda m: _cmd(m).pop("exit_code"),
        lambda m: _cmd(m).update(exit_code="0"),
        lambda m: _cmd(m).update(exit_code=None),
        lambda m: _cmd(m).pop("disk"),
        lambda m: _cmd(m).pop("sha256"),
        lambda m: _cmd(m).update(size="10"),
        lambda m: m["files"][0].update(sha256=1),
        lambda m: m["symlinks"][0].update(target=5),
        lambda m: m["symlinks"][0].pop("target"),
        lambda m: m["commands"].append({"argv": []}),
    ],
)
def test_malformed_manifest_is_snapshot_error(copy: Path, change: Any) -> None:
    edit_manifest(copy, change)
    with pytest.raises(SnapshotError):
        Snapshot.open(copy)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_post_open_fstat_refuses_a_non_regular_file(
    copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the race with a FIFO that serves the right bytes: only fstat can catch it."""
    import threading
    import time

    target = copy / "fs" / "proc" / "200" / "limits"
    data = target.read_bytes()
    regular = os.stat(target)
    target.unlink()
    os.mkfifo(target)
    snap = Snapshot.open(copy)
    real_lstat = os.lstat
    monkeypatch.setattr(os, "lstat", lambda p: regular if Path(p) == target else real_lstat(p))

    def writer() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                fd = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                time.sleep(0.01)  # no reader yet
                continue
            os.write(fd, data)
            os.close(fd)
            return

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    with pytest.raises(SnapshotError, match="not a regular file"):
        snap.read_file("/proc/200/limits")
    thread.join(6)
