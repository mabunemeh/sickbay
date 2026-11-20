"""Model providers. The agent loop sees plain dicts only; the SDK lives in providers.anthropic,
which is imported lazily, so replay, tests and CI need neither the SDK nor a network."""

from sickbay.agent.providers.base import (
    LATENCY_KEY,
    Provider,
    ProviderError,
    ProviderSetupError,
    ReplayMismatch,
    canonical_json,
    request_sha256,
)
from sickbay.agent.providers.replay import (
    RECORDING_SCHEMA_VERSION,
    RecordingError,
    RecordingProvider,
    ReplayProvider,
    dumps_recording,
    load_recording,
    recording_path,
)

__all__ = [
    "LATENCY_KEY",
    "RECORDING_SCHEMA_VERSION",
    "Provider",
    "ProviderError",
    "ProviderSetupError",
    "RecordingError",
    "RecordingProvider",
    "ReplayMismatch",
    "ReplayProvider",
    "canonical_json",
    "dumps_recording",
    "load_recording",
    "recording_path",
    "request_sha256",
]
