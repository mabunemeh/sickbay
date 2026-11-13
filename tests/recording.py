"""A dict-backed HostView that records every call, for tool tests."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sickbay.snapshot.hostview import CommandResult, HostView, Kind, UnknownCommandError


class RecordingView:
    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        symlinks: dict[str, str] | None = None,
        commands: dict[str, bytes] | None = None,
        journal: list[dict[str, Any]] | None = None,
    ) -> None:
        self.files = files or {}
        self.symlinks = symlinks or {}
        self.commands = commands or {}
        self.entries = journal or []
        self.calls: list[tuple[str, str]] = []

    def _children(self, path: str) -> set[str]:
        prefix = path.rstrip("/") + "/"
        return {
            p[len(prefix) :].split("/", 1)[0]
            for p in [*self.files, *self.symlinks]
            if p.startswith(prefix)
        }

    def kind(self, path: str) -> Kind | None:
        self.calls.append(("kind", path))
        if path in self.files:
            return "file"
        if path in self.symlinks:
            return "symlink"
        return "dir" if path == "/" or self._children(path) else None

    def read_file(self, path: str) -> bytes:
        self.calls.append(("read_file", path))
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def list_dir(self, path: str) -> list[str]:
        self.calls.append(("list_dir", path))
        return sorted(self._children(path))

    def readlink(self, path: str) -> str:
        self.calls.append(("readlink", path))
        return self.symlinks[path]

    def command(self, command_id: str) -> CommandResult:
        self.calls.append(("command", command_id))
        if command_id not in self.commands:
            raise UnknownCommandError(command_id)
        return CommandResult((command_id,), 0, self.commands[command_id])

    def journal(self) -> Iterator[dict[str, Any]]:
        self.calls.append(("journal", ""))
        yield from self.entries


def _check_is_hostview(view: RecordingView) -> HostView:
    return view
