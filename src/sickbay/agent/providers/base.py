"""The Provider protocol, its errors, and the canonical request hash."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

LATENCY_KEY = "_latency_s"


class Provider(Protocol):
    """One model endpoint. complete() takes the exact Messages API body and returns the
    response as a plain JSON-able dict plus LATENCY_KEY (float seconds)."""

    name: str

    def complete(self, request: dict[str, Any]) -> dict[str, Any]: ...


class ProviderError(Exception):
    """The provider failed after its own retries. The run stops (exit 4)."""

    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class ProviderSetupError(Exception):
    """The provider cannot be built: SDK missing or no key (exit 2)."""


class ReplayMismatch(Exception):
    """A replayed request differs from the recording, or the recording ran out (exit 3)."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_sha256(request: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest()
