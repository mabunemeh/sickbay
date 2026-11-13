"""Reads a snapshot directory back through the HostView interface."""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from sickbay.snapshot.hostview import CommandResult, HostView, Kind, UnknownCommandError
from sickbay.snapshot.pathenc import split_host_path
from sickbay.snapshot.writer import FORMAT, FORMAT_VERSION, MANIFEST, sha256_hex


class SnapshotError(Exception):
    """The snapshot is unreadable or malformed."""


class UnsupportedVersionError(SnapshotError):
    """The snapshot has a format or format_version this reader does not know."""


def _safe_disk(disk: object) -> str:
    if not isinstance(disk, str) or not disk:
        raise SnapshotError(f"bad disk path in manifest: {disk!r}")
    bad = disk.startswith("/") or "\\" in disk or ":" in disk or "\x00" in disk
    if bad or any(p in ("", ".", "..") for p in PurePosixPath(disk).parts):
        raise SnapshotError(f"unsafe disk path in manifest: {disk!r}")
    return disk


_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (Windows junctions, symlinks)
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)


def _plain(st: os.stat_result, want_dir: bool) -> bool:
    if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
        return False
    return stat.S_ISDIR(st.st_mode) if want_dir else stat.S_ISREG(st.st_mode)


def read_plain(root: Path, disk: str) -> bytes:
    """Read root/disk without following any link.

    Every component of the on-disk path below root must be a real directory or, last, a
    regular file (no symlink, no reparse point), and the resolved file must stay under the
    resolved root. Anything else is SnapshotError. The root itself may be reached via a link.
    """
    parts = disk.split("/")
    try:
        base = root.resolve(strict=True)
        current = base
        for i, part in enumerate(parts):
            current = current / part
            if not _plain(os.lstat(current), want_dir=i < len(parts) - 1):
                raise SnapshotError(f"not a plain file or directory in snapshot: {disk}")
        if not current.resolve(strict=True).is_relative_to(base):
            raise SnapshotError(f"path escapes the snapshot: {disk}")
        fd = os.open(current, _OPEN_FLAGS)
        with os.fdopen(fd, "rb") as f:
            if not _plain(os.fstat(f.fileno()), want_dir=False):
                raise SnapshotError(f"not a regular file in snapshot: {disk}")
            data = f.read()
    except OSError as exc:
        raise SnapshotError(f"cannot read {disk} from snapshot: {exc}") from exc
    return data


def read_checked(root: Path, entry: dict[str, Any]) -> bytes:
    """read_plain one manifest entry, then check its size and sha256."""
    disk = entry["disk"]
    data = read_plain(root, disk)
    if "size" in entry and len(data) != entry["size"]:
        raise SnapshotError(f"size mismatch: {disk}")
    if sha256_hex(data) != entry["sha256"]:
        raise SnapshotError(f"sha256 mismatch: {disk}")
    return data


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class Snapshot:
    def __init__(self, root: Path, manifest: dict[str, Any], manifest_bytes: bytes) -> None:
        self.root = root
        self.manifest = manifest
        self._manifest_bytes = manifest_bytes
        try:
            self._files: dict[str, dict[str, Any]] = {e["path"]: e for e in manifest["files"]}
            self._symlinks: dict[str, str] = {e["path"]: e["target"] for e in manifest["symlinks"]}
            self._commands: dict[str, dict[str, Any]] = {e["id"]: e for e in manifest["commands"]}
            self._journal: dict[str, Any] = manifest["journal"]
            for entry in [*self._files.values(), *self._commands.values(), self._journal]:
                _safe_disk(entry["disk"])
                if not isinstance(entry["sha256"], str):
                    raise SnapshotError("malformed manifest: sha256 is not a string")
                if "size" in entry and not _count(entry["size"]):
                    raise SnapshotError("malformed manifest: size is not a count")
            for entry in self._commands.values():
                argv, code = entry["argv"], entry["exit_code"]
                if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
                    raise SnapshotError(f"malformed manifest: argv of {entry['id']!r}")
                if not isinstance(code, int) or isinstance(code, bool):
                    raise SnapshotError(f"malformed manifest: exit_code of {entry['id']!r}")
            if not _count(self._journal["entries"]):
                raise SnapshotError("malformed manifest: journal entries is not a count")
            if not all(isinstance(t, str) for t in self._symlinks.values()):
                raise SnapshotError("malformed manifest: symlink target is not a string")
            self._dirs: dict[str, set[str]] = {"/": set()}
            for path in [*self._files, *self._symlinks]:
                self._index(path)
            for path in self._files:
                if path in self._symlinks or path in self._dirs:
                    raise SnapshotError(f"manifest path is listed twice: {path}")
            for path in self._symlinks:
                if path in self._dirs:
                    raise SnapshotError(f"manifest symlink is also a directory: {path}")
        except (KeyError, TypeError, AttributeError) as exc:
            raise SnapshotError(f"malformed manifest: {exc!r}") from exc

    def _index(self, path: str) -> None:
        parts = split_host_path(path)
        if not parts:
            raise SnapshotError("manifest lists the root path")
        parent = "/"
        for part in parts[:-1]:
            self._dirs[parent].add(part)
            parent = parent.rstrip("/") + "/" + part
            self._dirs.setdefault(parent, set())
        self._dirs[parent].add(parts[-1])

    @classmethod
    def open(cls, path: str | Path) -> Snapshot:
        root = Path(path)
        raw = read_plain(root, MANIFEST)  # no hash for the manifest: the no-link check gates it
        try:
            manifest = json.loads(raw)
        except ValueError as exc:
            raise SnapshotError(f"cannot read {MANIFEST} in {root}: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise UnsupportedVersionError("not a sickbay snapshot")
        version = manifest.get("format_version")
        if version != FORMAT_VERSION:
            raise UnsupportedVersionError(f"unsupported format_version: {version!r}")
        return cls(root, manifest, raw)

    # -- integrity ---------------------------------------------------------------------------

    def _entries(self) -> list[tuple[str, dict[str, Any]]]:
        entries = [(f"file {e['path']}", e) for e in self._files.values()]
        entries += [(f"command {e['id']}", e) for e in self._commands.values()]
        entries.append(("journal", self._journal))
        return entries

    def verify(self) -> list[str]:
        """Return a list of problems; empty means every hash matches and nothing is stray."""
        problems: list[str] = []
        expected = {MANIFEST}
        for label, entry in self._entries():
            disk = entry["disk"]
            expected.add(disk)
            target = self.root.joinpath(*disk.split("/"))
            if target.is_symlink() or not target.is_file():
                problems.append(f"{label}: missing or not a regular file: {disk}")
                continue
            data = target.read_bytes()
            if "size" in entry and len(data) != entry["size"]:
                problems.append(f"{label}: size {len(data)} != {entry['size']}")
            if sha256_hex(data) != entry["sha256"]:
                problems.append(f"{label}: sha256 mismatch")
            elif label == "journal" and data.count(b"\n") != entry["entries"]:
                problems.append("journal: entry count mismatch")
        for p in sorted(self.root.rglob("*")):
            if p.is_dir() and not p.is_symlink():
                continue
            rel = p.relative_to(self.root).as_posix()
            if rel not in expected:
                problems.append(f"unexpected file not in manifest: {rel}")
        return problems

    # -- HostView ------------------------------------------------------------------------------

    def read_file(self, path: str) -> bytes:
        split_host_path(path)
        if path in self._files:
            return read_checked(self.root, self._files[path])
        if path in self._symlinks:
            raise OSError(f"{path} is a symlink; use readlink")
        if path in self._dirs:
            raise IsADirectoryError(path)
        raise FileNotFoundError(path)

    def list_dir(self, path: str) -> list[str]:
        split_host_path(path)
        if path in self._dirs:
            return sorted(self._dirs[path])
        if path in self._files or path in self._symlinks:
            raise NotADirectoryError(path)
        raise FileNotFoundError(path)

    def readlink(self, path: str) -> str:
        split_host_path(path)
        if path in self._symlinks:
            return self._symlinks[path]
        raise FileNotFoundError(path)

    def kind(self, path: str) -> Kind | None:
        split_host_path(path)
        if path in self._files:
            return "file"
        if path in self._symlinks:
            return "symlink"
        if path in self._dirs:
            return "dir"
        return None

    def command(self, command_id: str) -> CommandResult:
        try:
            entry = self._commands[command_id]
        except KeyError:
            raise UnknownCommandError(command_id) from None
        stdout = read_checked(self.root, entry)
        return CommandResult(tuple(entry["argv"]), entry["exit_code"], stdout)

    def journal(self) -> Iterator[dict[str, Any]]:
        data = read_checked(self.root, self._journal)
        if data.count(b"\n") != self._journal["entries"]:
            raise SnapshotError("journal: entry count mismatch")
        for line in data.splitlines():
            if line:
                try:
                    entry = json.loads(line)
                except ValueError as exc:
                    raise SnapshotError(f"journal: unparseable line: {exc}") from exc
                if not isinstance(entry, dict):
                    raise SnapshotError("journal: line is not a JSON object")
                yield entry

    # -- copy ----------------------------------------------------------------------------------

    def write(self, dest: str | Path) -> None:
        """Write a byte-identical tree to dest, which must not exist or be empty."""
        out = Path(dest)
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            raise FileExistsError(f"destination exists and is not empty: {out}")
        out.mkdir(parents=True, exist_ok=True)
        (out / MANIFEST).write_bytes(self._manifest_bytes)
        for _, entry in self._entries():
            parts = entry["disk"].split("/")
            target = out.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root.joinpath(*parts), target)


def _check_snapshot_is_hostview(snap: Snapshot) -> HostView:
    return snap
