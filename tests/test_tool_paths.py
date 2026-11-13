"""Allowlist, path-traversal and symlink-escape probes for the tools."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from recording import RecordingView

from sickbay.snapshot import Snapshot, SnapshotError
from sickbay.tools import call_tool
from sickbay.tools.paths import is_listable, is_readable

FIXTURE = Path(__file__).parent / "fixtures" / "basic"
REJECTED = {"not_allowed", "invalid_argument"}


def busy_view() -> RecordingView:
    """A view holding the very files the probes aim at, so a leak would be visible."""
    return RecordingView(
        files={
            "/etc/shadow": b"root:SECRET",
            "/proc/1/environ": b"SECRET=1",
            "/proc/self/limits": b"x",
            "/proc/1/limits": b"x",
            "/root/.ssh/id_rsa": b"SECRET",
            "/home/u/x": b"SECRET",
            "/etc/systemd/system/a.service": b"[Service]\n",
        },
        commands={"systemctl-show": b"Id=a.service\n"},
        journal=[{"MESSAGE": "x", "UNIT": "a.service"}],
    )


PATH_PROBES = [
    "..",
    "../etc/shadow",
    "/..",
    "/etc/../etc/shadow",
    "/etc/systemd/../shadow",
    "/etc/systemd/system/../../shadow",
    "//etc",
    "//etc/systemd",
    "/etc//systemd",
    "/etc/./x",
    "/etc/systemd/./system",
    "/etc/systemd/.",
    "/%2e%2e/etc/shadow",
    "/etc/systemd/%2e%2e/%2e%2e/shadow",
    "/etc/systemd/%2E%2E",
    "/etc/systemd/%2fetc%2fshadow",
    "\\etc\\shadow",
    "/etc/systemd\\..\\..\\shadow",
    "/etc/systemd/system/a.service\x00",
    "/etc/systemd/\x00/../shadow",
    "/etc/systemd/\n",
    "/etc/systemd/\x7f",
    "/etc/systemd/\x85",
    "C:\\",
    "C:\\Windows\\win.ini",
    "C:/Windows",
    "c:",
    "etc/shadow",
    "./etc/systemd",
    "",
    " /etc/systemd",
    "~root",
    "/etc/systemd/",
    "/etc/systemd/" + "a" * 250,
    "/" + "é" * 128,
    "/etc/shadow",
    "/etc/passwd",
    "/root",
    "/root/.ssh/id_rsa",
    "/home",
    "/home/u/x",
    "/proc/1/environ",
    "/proc/self/limits",
    "/proc/self/environ",
    "/proc/thread-self/limits",
    "/proc/01/limits",
    "/proc/1a/limits",
    "/proc/1/limits/x/..",
    "/proc/1/task/1/limits",
    "/proc/sys/kernel/hostname",
    "/proc/sysrq-trigger",
    "/sys/fs/cgroupx/memory.max",
    "/sys/fs",
    "/run/systemd/system.controlx/a",
    "/run/systemd",
    "/etc/systemdx/a",
    "/etc/security/limits.conf.d/a",
    "/etc/os-release/x",
    "/usr/lib/os-release",
]


@pytest.mark.parametrize("path", PATH_PROBES)
def test_read_file_path_probe_never_touches_the_view(path: str) -> None:
    view = busy_view()
    result = call_tool(view, "read_file", {"path": path})
    assert result["error"]["kind"] in REJECTED, result
    assert view.calls == []
    assert "SECRET" not in str(result)
    assert len(result["error"]["message"].encode()) < 256 + 128


LIST_ONLY_OK = {"/sys/fs", "/run/systemd"}  # ancestors of allowed trees may be listed


@pytest.mark.parametrize("path", [p for p in PATH_PROBES if p not in LIST_ONLY_OK])
def test_list_dir_path_probe_never_touches_the_view(path: str) -> None:
    view = busy_view()
    result = call_tool(view, "list_dir", {"path": path})
    assert result["error"]["kind"] in REJECTED, result
    assert view.calls == []


@pytest.mark.parametrize(
    "pid",
    ["1/../..", "../1", "1/../../etc/shadow", "self", "-1", "0", "1 ", " 1", "1\x00", "", "9" * 11],
)
def test_pid_probe_never_touches_the_view(pid: str) -> None:
    view = busy_view()
    result = call_tool(view, "process_limits", {"pid": pid})
    assert result["error"]["kind"] == "invalid_argument"
    assert view.calls == []


@pytest.mark.parametrize(
    "unit", ["../x", "a/b.service", "/etc/shadow", "", ".", "..", "a b.service", "x\x00.service"]
)
@pytest.mark.parametrize("tool", ["unit_status", "journal_query"])
def test_unit_probe_never_touches_the_view(tool: str, unit: str) -> None:
    view = busy_view()
    result = call_tool(view, tool, {"unit": unit})
    assert result["error"]["kind"] == "invalid_argument"
    assert view.calls == []


@pytest.mark.parametrize(
    "path", ["..", "/..", "/../../etc", "system.slice", "/system.slice/", "//", "\\a", "/%2e%2e"]
)
def test_cgroup_path_probe_never_touches_the_view(path: str) -> None:
    view = busy_view()
    result = call_tool(view, "cgroup_tree", {"path": path})
    assert result["error"]["kind"] in REJECTED
    assert view.calls == []


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("read_file", {"path": "/etc/systemd/x\\..\\..\\shadow"}),
        ("read_file", {"path": "/etc/systemd/..\\..\\shadow"}),
        ("list_dir", {"path": "/etc/systemd/..\\..\\root"}),
        ("cgroup_tree", {"path": "/..\\..\\..\\etc"}),
        ("cgroup_tree", {"path": "/a\\b"}),
    ],
)
def test_backslash_under_an_allowed_tree_is_a_literal_name(name: str, args: dict[str, Any]) -> None:
    """Allowed, so the view is asked, but for a literal file name that does not exist."""
    view = busy_view()
    result = call_tool(view, name, args)
    assert result["error"]["kind"] == "not_found"
    assert not any(c[0] in ("read_file", "list_dir") for c in view.calls)
    assert "SECRET" not in str(result)


def test_real_x2d_path_in_an_incident_is_readable() -> None:
    root = Path(__file__).parent.parent / "incidents" / "fd-exhaustion" / "snapshot"
    if not (root / "manifest.json").exists():
        pytest.skip("incident snapshot not present")
    snap = Snapshot.open(root)
    unit = "snap-gtk\\x2dcommon\\x2dthemes-1535.mount"
    result = call_tool(snap, "read_file", {"path": f"/etc/systemd/system/{unit}"})
    assert result["size"] > 0 and "[Mount]" in result["content"]
    listing = call_tool(snap, "list_dir", {"path": f"/sys/fs/cgroup/system.slice/{unit}"})
    assert "cgroup.controllers" in [e["name"] for e in listing["entries"]]
    tree = call_tool(snap, "cgroup_tree", {"path": f"/system.slice/{unit}"})
    assert tree["nodes"][0]["path"] == f"/system.slice/{unit}"


def test_allowlist_rules() -> None:
    for path in [
        "/proc/meminfo",
        "/proc/pressure/memory",
        "/proc/sys/fs/file-nr",
        "/proc/123/limits",
        "/proc/123/fd",
        "/sys/fs/cgroup",
        "/sys/fs/cgroup/a.slice/b/memory.max",
        "/etc/systemd/system.conf",
        "/etc/security/limits.conf",
        "/etc/security/limits.d/x.conf",
        "/etc/os-release",
        "/run/systemd/transient/x.service",
        "/run/systemd/system.control/x.service.d/50-MemoryMax.conf",
    ]:
        assert is_readable(path), path
    for path in ["/", "/proc", "/proc/123", "/proc/sys", "/etc", "/etc/security", "/sys", "/run"]:
        assert not is_readable(path) and is_listable(path), path
    for path in ["/proc/123/environ", "/proc/self", "/usr", "/etc/shadow", "/proc/sys/kernel"]:
        assert not is_listable(path), path


# -- symlinks, layer (a): manifest / HostView symlinks are never followed -----------------------


def test_symlink_leaf_pointing_outside_is_reported_not_followed() -> None:
    view = RecordingView(
        files={"/etc/shadow": b"SECRET"},
        symlinks={"/etc/systemd/system/evil.service": "/etc/shadow"},
    )
    result = call_tool(view, "read_file", {"path": "/etc/systemd/system/evil.service"})
    assert result["error"]["kind"] == "is_symlink" and result["target"] == "/etc/shadow"
    assert ("read_file", "/etc/shadow") not in view.calls
    assert not any(c[0] == "read_file" for c in view.calls)


def test_symlinked_parent_directory_is_not_traversed() -> None:
    view = RecordingView(
        files={"/etc/systemd/system/x.conf": b"SECRET"},
        symlinks={"/etc/systemd": "/root"},
    )
    for name, args in [
        ("read_file", {"path": "/etc/systemd/system/x.conf"}),
        ("list_dir", {"path": "/etc/systemd/system"}),
        ("list_dir", {"path": "/etc/systemd"}),
    ]:
        result = call_tool(view, name, args)
        assert result["error"]["kind"] == "is_symlink", (name, result)
        assert result["target"] == "/root"
    assert not any(c[0] in ("read_file", "list_dir") for c in view.calls)


def test_symlinked_cgroup_and_fd_are_not_followed() -> None:
    view = RecordingView(
        files={"/root/memory.max": b"SECRET", "/proc/5/limits": b""},
        symlinks={"/sys/fs/cgroup/evil.slice": "/root", "/proc/5/fd": "/root"},
    )
    tree = call_tool(view, "cgroup_tree", {})
    assert [n["path"] for n in tree["nodes"]] == ["/"]
    assert call_tool(view, "cgroup_tree", {"path": "/evil.slice"})["error"]["kind"] == "is_symlink"
    assert call_tool(view, "process_limits", {"pid": "5"})["error"]["kind"] == "is_symlink"
    assert ("read_file", "/root/memory.max") not in view.calls


def test_fixture_manifest_symlink_outside_allowlist() -> None:
    snap = Snapshot.open(FIXTURE)
    result = call_tool(snap, "read_file", {"path": "/etc/os-release"})
    assert result["error"]["kind"] == "is_symlink"
    assert result["target"] == "../usr/lib/os-release"


# -- symlinks, layer (b): real OS symlinks planted inside the snapshot directory ----------------


def _symlink(link: Path, target: Path, *, is_dir: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks here: {exc}")


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    dest = tmp_path / "snap"
    shutil.copytree(FIXTURE, dest)
    return dest


def _swap_file_for_link(copy: Path, rel: str, outside: Path) -> None:
    target = copy / rel
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(target.read_bytes())  # same bytes, so only the link can fail it
    target.unlink()
    _symlink(target, outside)


@pytest.mark.parametrize(
    ("rel", "name", "args"),
    [
        ("fs/proc/200/limits", "read_file", {"path": "/proc/200/limits"}),
        ("fs/proc/200/limits", "process_limits", {"pid": "200"}),
        ("commands/ps.txt", "process_list", {}),
        ("journal.jsonl", "journal_query", {}),
    ],
)
def test_os_symlink_file_in_snapshot_raises(
    copy: Path, tmp_path: Path, rel: str, name: str, args: dict[str, Any]
) -> None:
    _swap_file_for_link(copy, rel, tmp_path / "outside" / "f")
    snap = Snapshot.open(copy)
    with pytest.raises(SnapshotError):
        call_tool(snap, name, args)


def test_os_symlink_parent_directory_in_snapshot_raises(copy: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "200"
    shutil.copytree(copy / "fs" / "proc" / "200", outside)
    shutil.rmtree(copy / "fs" / "proc" / "200")
    _symlink(copy / "fs" / "proc" / "200", outside, is_dir=True)
    snap = Snapshot.open(copy)
    with pytest.raises(SnapshotError):
        snap.read_file("/proc/200/limits")
    with pytest.raises(SnapshotError):
        call_tool(snap, "read_file", {"path": "/proc/200/limits"})


def test_os_symlinked_snapshot_root_is_fine(copy: Path, tmp_path: Path) -> None:
    """The root itself may be reached through a link; only links inside it are refused."""
    link = tmp_path / "via-link"
    _symlink(link, copy, is_dir=True)
    assert call_tool(Snapshot.open(link), "read_file", {"path": "/proc/200/limits"})["size"] > 0
