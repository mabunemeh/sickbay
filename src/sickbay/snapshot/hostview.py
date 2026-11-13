"""The read interface shared by snapshots and live hosts."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

Kind = Literal["file", "dir", "symlink", "other"]


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: bytes


class UnknownCommandError(KeyError):
    """The command id is not one this host view has (not captured, or not on the list)."""


class HostView(Protocol):
    """What tools may ask of a host. Paths are absolute, normalized host paths."""

    def read_file(self, path: str) -> bytes: ...

    def list_dir(self, path: str) -> list[str]: ...

    def readlink(self, path: str) -> str: ...

    def kind(self, path: str) -> Kind | None:
        """The entry type at path, without following a symlink; None if absent."""
        ...

    def command(self, command_id: str) -> CommandResult:
        """Raises UnknownCommandError, and only that KeyError, for an unknown command id."""
        ...

    def journal(self) -> Iterator[dict[str, Any]]: ...
