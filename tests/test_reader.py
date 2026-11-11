import json
import shutil
from pathlib import Path

import pytest

from sickbay.snapshot import Snapshot, SnapshotError, UnsupportedVersionError
from sickbay.snapshot.pathenc import InvalidHostPathError

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
