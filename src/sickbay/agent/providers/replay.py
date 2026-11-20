"""Recording a provider's exchanges, and replaying them without a network.

A recording file (recording_schema 1) sits beside its result file:

    {"recording_schema": 1, "result_file": "<name>", "predictor": "...", "provider": "...",
     "model": "...", "effort": "...", "prompt_version": "...", "max_usd": 5.0, "repeats": 1,
     "cases": [{"incident": "...", "repeat": 0,
                "exchanges": [{"request_sha256": "...", "response": {...}, "latency_s": 1.234}]}]}

The runner calls start_case(incident, repeat) before each case it starts, so exchanges are
grouped per case and a replay can name the case where it diverged.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from sickbay.agent.providers.base import LATENCY_KEY, Provider, ReplayMismatch, request_sha256

RECORDING_SCHEMA_VERSION = 1

# Header keys a recording must carry, besides recording_schema and cases.
HEADER_KEYS = (
    "result_file",
    "predictor",
    "provider",
    "model",
    "effort",
    "prompt_version",
    "max_usd",
    "repeats",
)


class RecordingError(ValueError):
    """A recording file is unreadable or not recording_schema 1 (exit 3)."""


class RecordingProvider:
    """Wraps any provider and appends every exchange to the current case."""

    def __init__(self, inner: Provider) -> None:
        self.inner = inner
        self.name = inner.name
        self.cases: list[dict[str, Any]] = []

    def start_case(self, incident: str, repeat: int) -> None:
        self.cases.append({"incident": incident, "repeat": repeat, "exchanges": []})

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.cases:
            raise RuntimeError("RecordingProvider.complete() before start_case()")
        digest = request_sha256(request)
        response = self.inner.complete(request)
        recorded = {k: v for k, v in response.items() if k != LATENCY_KEY}
        self.cases[-1]["exchanges"].append(
            {
                "request_sha256": digest,
                "response": copy.deepcopy(recorded),
                "latency_s": float(response.get(LATENCY_KEY, 0.0)),
            }
        )
        return response

    def document(self, **header: Any) -> dict[str, Any]:
        """The recording file's content; header must hold exactly HEADER_KEYS."""
        if set(header) != set(HEADER_KEYS):
            raise ValueError(f"recording header keys must be {HEADER_KEYS}")
        return {"recording_schema": RECORDING_SCHEMA_VERSION, **header, "cases": self.cases}


class ReplayProvider:
    """Returns recorded responses in order; any divergence raises ReplayMismatch."""

    name = "replay"

    def __init__(self, cases: list[dict[str, Any]]) -> None:
        self._cases = {(c["incident"], c["repeat"]): c["exchanges"] for c in cases}
        self._unvisited = set(self._cases)
        self._case: tuple[str, int] | None = None
        self._exchanges: list[dict[str, Any]] = []
        self._index = 0

    def _where(self) -> str:
        assert self._case is not None
        return f"incident {self._case[0]} repeat {self._case[1]} exchange {self._index}"

    def _check_consumed(self) -> None:
        if self._case is not None and self._index < len(self._exchanges):
            raise ReplayMismatch(
                f"replay mismatch: {self._where()}: the run stopped before this recorded "
                f"exchange ({len(self._exchanges)} recorded)"
            )

    def start_case(self, incident: str, repeat: int) -> None:
        self._check_consumed()
        key = (incident, repeat)
        if key not in self._cases:
            raise ReplayMismatch(
                f"replay mismatch: incident {incident} repeat {repeat} is not in the recording"
            )
        self._unvisited.discard(key)
        self._case, self._exchanges, self._index = key, self._cases[key], 0

    def finish(self) -> None:
        """Raise ReplayMismatch if any recorded exchange or case was not replayed."""
        self._check_consumed()
        if self._unvisited:
            incident, repeat = sorted(self._unvisited)[0]
            raise ReplayMismatch(
                f"replay mismatch: incident {incident} repeat {repeat} is recorded but was not run"
            )

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._case is None:
            raise ReplayMismatch("replay mismatch: a request before any case started")
        if self._index >= len(self._exchanges):
            raise ReplayMismatch(
                f"replay mismatch: {self._where()}: the recording has no more exchanges "
                f"({len(self._exchanges)} recorded)"
            )
        exchange = self._exchanges[self._index]
        if request_sha256(request) != exchange["request_sha256"]:
            raise ReplayMismatch(
                f"replay mismatch: {self._where()}: the request differs from the recording"
            )
        self._index += 1
        response: dict[str, Any] = copy.deepcopy(exchange["response"])
        response[LATENCY_KEY] = exchange["latency_s"]
        return response


def dumps_recording(document: dict[str, Any]) -> bytes:
    """Serialised like a result: sorted keys, indent 2, UTF-8, LF, trailing newline."""
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
    return (text + "\n").encode("utf-8")


def recording_path(result_path: Path) -> Path:
    return result_path.with_suffix(".recording.json")


def _fail(path: Path, message: str) -> RecordingError:
    return RecordingError(f"{path}: {message}")


def load_recording(path: Path) -> dict[str, Any]:
    """Read and shape-check a recording file; RecordingError on any problem."""
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _fail(path, f"cannot read recording: {exc}") from exc
    if not isinstance(data, dict):
        raise _fail(path, "recording must be a JSON object")
    if data.get("recording_schema") != RECORDING_SCHEMA_VERSION:
        raise _fail(path, f"recording_schema must be {RECORDING_SCHEMA_VERSION}")
    expected = {"recording_schema", "cases", *HEADER_KEYS}
    if set(data) != expected:
        raise _fail(path, f"recording keys must be {sorted(expected)}")
    for key in ("predictor", "provider", "model", "effort", "prompt_version"):
        if not isinstance(data[key], str):
            raise _fail(path, f"{key} must be a string")
    repeats = data["repeats"]
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
        raise _fail(path, "repeats must be a positive integer")
    max_usd = data["max_usd"]
    if max_usd is not None and (isinstance(max_usd, bool) or not isinstance(max_usd, int | float)):
        raise _fail(path, "max_usd must be a number or null")
    cases = data["cases"]
    if not isinstance(cases, list):
        raise _fail(path, "cases must be a list")
    for i, case in enumerate(cases):
        if not (
            isinstance(case, dict)
            and set(case) == {"incident", "repeat", "exchanges"}
            and isinstance(case["incident"], str)
            and isinstance(case["repeat"], int)
            and isinstance(case["exchanges"], list)
        ):
            raise _fail(path, f"cases[{i}] must be {{incident, repeat, exchanges}}")
        for j, ex in enumerate(case["exchanges"]):
            if not (
                isinstance(ex, dict)
                and set(ex) == {"request_sha256", "response", "latency_s"}
                and isinstance(ex["request_sha256"], str)
                and isinstance(ex["response"], dict)
                and isinstance(ex["latency_s"], int | float)
            ):
                raise _fail(path, f"cases[{i}].exchanges[{j}] is malformed")
    return data
