"""Writes a snapshot directory deterministically."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sickbay.snapshot.pathenc import disk_path

FORMAT = "sickbay-snapshot"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
JOURNAL = "journal.jsonl"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dump_manifest(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")


class SnapshotWriter:
    def __init__(self, dest: Path) -> None:
        self.dest = dest
        self._files: dict[str, dict[str, Any]] = {}
        self._symlinks: dict[str, str] = {}
        self._commands: dict[str, dict[str, Any]] = {}
        self._errors: list[dict[str, str]] = []
        self._journal: dict[str, Any] | None = None
        dest.mkdir(parents=True, exist_ok=True)

    def _write(self, disk: str, data: bytes) -> None:
        target = self.dest.joinpath(*disk.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def add_file(self, path: str, data: bytes, note: str | None = None) -> None:
        if path in self._files or path in self._symlinks:
            raise ValueError(f"duplicate snapshot path: {path}")
        disk = disk_path(path)
        self._write(disk, data)
        entry: dict[str, Any] = {
            "path": path,
            "disk": disk,
            "size": len(data),
            "sha256": sha256_hex(data),
        }
        if note:
            entry["note"] = note
        self._files[path] = entry

    def add_symlink(self, path: str, target: str) -> None:
        if path in self._files or path in self._symlinks:
            raise ValueError(f"duplicate snapshot path: {path}")
        self._symlinks[path] = target

    def add_command(self, command_id: str, argv: list[str], exit_code: int, stdout: bytes) -> None:
        disk = f"commands/{command_id}.txt"
        self._write(disk, stdout)
        self._commands[command_id] = {
            "id": command_id,
            "argv": argv,
            "exit_code": exit_code,
            "disk": disk,
            "size": len(stdout),
            "sha256": sha256_hex(stdout),
        }

    def add_error(self, error: str, *, path: str | None = None, ident: str | None = None) -> None:
        entry = {"error": error}
        if path is not None:
            entry["path"] = path
        if ident is not None:
            entry["id"] = ident
        self._errors.append(entry)

    def write_journal(self, lines: list[bytes], since: str) -> None:
        data = b"".join(line + b"\n" for line in lines)
        self._write(JOURNAL, data)
        self._journal = {
            "disk": JOURNAL,
            "since": since,
            "entries": len(lines),
            "sha256": sha256_hex(data),
        }

    def finish(
        self,
        *,
        captured_at: str,
        scrubbed: bool,
        host: dict[str, str],
        excluded_pids_count: int,
        scrub_counts: dict[str, int],
    ) -> dict[str, Any]:
        if self._journal is None:
            self.write_journal([], "")
        manifest: dict[str, Any] = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "captured_at": captured_at,
            "scrubbed": scrubbed,
            "scrub_counts": dict(sorted(scrub_counts.items())),
            "host": host,
            "files": [self._files[p] for p in sorted(self._files)],
            "symlinks": [{"path": p, "target": self._symlinks[p]} for p in sorted(self._symlinks)],
            "commands": [self._commands[c] for c in sorted(self._commands)],
            "journal": self._journal,
            "errors": sorted(
                self._errors,
                key=lambda e: (e.get("path", e.get("id", "")), e["error"]),
            ),
            "excluded_pids_count": excluded_pids_count,
        }
        self._write(MANIFEST, dump_manifest(manifest))
        return manifest
