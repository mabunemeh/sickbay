"""Small helpers for the eval tests: a journal-only HostView and label.yaml text."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import yaml

from sickbay.snapshot.commands import LIST_UNITS_ID
from sickbay.snapshot.hostview import CommandResult, Kind, UnknownCommandError

LIST_UNITS_TEXT = (
    "  app.service   loaded failed     failed  An app\n"
    "  web.service   loaded active     running A web server\n"
    "  aa.socket     loaded activating start   A socket\n"
)


class JournalView:
    """A HostView with a journal and a list-units output, and nothing else."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries

    def read_file(self, path: str) -> bytes:
        raise FileNotFoundError(path)

    def list_dir(self, path: str) -> list[str]:
        raise FileNotFoundError(path)

    def readlink(self, path: str) -> str:
        raise FileNotFoundError(path)

    def kind(self, path: str) -> Kind | None:
        return None

    def command(self, command_id: str) -> CommandResult:
        if command_id == LIST_UNITS_ID:
            return CommandResult(("systemctl",), 0, LIST_UNITS_TEXT.encode())
        raise UnknownCommandError(command_id)

    def journal(self) -> Iterator[dict[str, Any]]:
        yield from self.entries


def entry(message: str, usec: int = 1_000_000, **fields: str) -> dict[str, Any]:
    return {"MESSAGE": message, "__REALTIME_TIMESTAMP": str(usec), **fields}


def label_data(incident: str = "case-a", **changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "label_schema": 1,
        "status": "draft",
        "id": incident,
        "split": "dev",
        "injection": False,
        "title": "A title",
        "root_cause_category": "fd_limit",
        "component": "app.service",
        "also_accepted": [],
        "summary": "A summary.",
        "evidence": [{"source": "journal", "contains": "started"}],
        "fix": "A fix.",
        "leak_strings": ["leak"],
        "names": ["app"],
    }
    data.update(changes)
    return data


def label_yaml(incident: str = "case-a", **changes: Any) -> str:
    return yaml.safe_dump(label_data(incident, **changes), sort_keys=False)
