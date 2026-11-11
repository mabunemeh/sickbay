"""The read interface shared by snapshots and live hosts."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: bytes


class HostView(Protocol):
    """What tools may ask of a host. Paths are absolute, normalized host paths."""

    def read_file(self, path: str) -> bytes: ...

    def list_dir(self, path: str) -> list[str]: ...

    def readlink(self, path: str) -> str: ...

    def command(self, command_id: str) -> CommandResult: ...

    def journal(self) -> Iterator[dict[str, Any]]: ...
