"""The Anthropic Messages API provider. The ONLY module that imports the SDK.

The endpoint is pinned to API_BASE_URL: an ambient ANTHROPIC_BASE_URL is ignored. The key
comes from ANTHROPIC_API_KEY only and is never written anywhere. The SDK's own retries
(max_retries=2) are the only retries; nothing here logs requests, responses or headers.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

from sickbay.agent.providers.base import LATENCY_KEY, ProviderError, ProviderSetupError

if TYPE_CHECKING:
    import anthropic as sdk

API_BASE_URL = "https://api.anthropic.com"
MAX_RETRIES = 2
INSTALL_HINT = "pip install 'sickbay[anthropic]'"
KEY_ENV = "ANTHROPIC_API_KEY"
# 408 request timeout, 409 conflict, 429 rate limit, and any 5xx are worth retrying.
_RETRYABLE_STATUS = frozenset({408, 409, 429})


def _import_sdk() -> Any:
    try:
        import anthropic
    except ImportError:
        raise ProviderSetupError(f"the anthropic SDK is not installed: {INSTALL_HINT}") from None
    return anthropic


def map_error(exc: BaseException) -> ProviderError:
    """An SDK exception as a ProviderError (message without headers or key)."""
    sdk = _import_sdk()
    if isinstance(exc, sdk.APIStatusError):
        status = exc.status_code
        retryable = status in _RETRYABLE_STATUS or status >= 500
        return ProviderError(f"{type(exc).__name__} (HTTP {status}): {exc.message}", retryable)
    if isinstance(exc, sdk.APIConnectionError):  # includes APITimeoutError
        return ProviderError(f"{type(exc).__name__}: {exc.message}", True)
    return ProviderError(f"{type(exc).__name__}: {exc}", False)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: sdk.Anthropic) -> None:
        self._client = client

    @classmethod
    def from_env(cls) -> AnthropicProvider:
        """Build the client from ANTHROPIC_API_KEY; ProviderSetupError if it cannot."""
        sdk = _import_sdk()
        key = os.environ.get(KEY_ENV, "")
        if not key:
            raise ProviderSetupError(f"set {KEY_ENV}")
        client = sdk.Anthropic(
            api_key=key,
            base_url=API_BASE_URL,
            max_retries=MAX_RETRIES,
            open_telemetry=False,
        )
        return cls(client)

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        sdk = _import_sdk()
        start = time.monotonic()
        try:
            response = self._client.messages.create(**request)
        except sdk.AnthropicError as exc:
            raise map_error(exc) from None
        latency = round(time.monotonic() - start, 3)
        return normalise(response.to_dict(mode="json"), latency)


def normalise(data: dict[str, Any], latency_s: float) -> dict[str, Any]:
    """A plain JSON round trip, so a live response equals its replayed copy exactly."""
    plain: dict[str, Any] = json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))
    plain[LATENCY_KEY] = latency_s
    return plain
