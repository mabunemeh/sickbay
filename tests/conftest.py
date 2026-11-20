"""Every test runs with outbound network refused: no test may open a connection."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest


class NetworkBlocked(OSError):
    pass


def _refuse(*args: Any, **kwargs: Any) -> Any:
    raise NetworkBlocked("network access is blocked in tests")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    yield
