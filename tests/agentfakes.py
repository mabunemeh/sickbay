"""A scripted fake provider and Messages API response builders, for the agent tests."""

from __future__ import annotations

import copy
import json
from typing import Any

from sickbay.agent.providers import LATENCY_KEY

DIAGNOSIS: dict[str, Any] = {
    "root_cause_category": "fd_limit",
    "component": "ledger-api.service",
    # c1 is journal_query {} on tests/fixtures/basic in the scripted runs.
    "evidence": [{"call_id": "c1", "quote": "connect from 192.0.2.2 failed"}],
    "fix": "Raise LimitNOFILE.",
    "confidence": 0.75,
}


def usage(
    input_tokens: int = 1000,
    output_tokens: int = 100,
    cache_write: int = 0,
    cache_read: int = 0,
) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
    }


def response(
    stop_reason: str | None, content: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    return {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": extra.pop("usage", usage()),
        **extra,
    }


def thinking(text: str = "Look at the failed units first.") -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": "c2lnbmF0dXJlLWJ5dGVz+/=="}


def tool_use(tool_id: str, name: Any, args: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": args}


def tool_turn(*blocks: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return response("tool_use", list(blocks), **extra)


def final(diagnosis: Any = None, text: str | None = None, **extra: Any) -> dict[str, Any]:
    body = text if text is not None else json.dumps(diagnosis or DIAGNOSIS)
    return response("end_turn", [{"type": "text", "text": body}], **extra)


class FakeProvider:
    """Returns scripted responses in order; records a deep copy of every request."""

    name = "fake"

    def __init__(self, responses: list[dict[str, Any]], latency: float = 0.25) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.latency = latency

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(request))
        if not self.responses:
            raise AssertionError("FakeProvider ran out of scripted responses")
        out = copy.deepcopy(self.responses.pop(0))
        out[LATENCY_KEY] = self.latency
        return out


class FailingProvider:
    """Raises the given exception on call number `fail_at` (0-based)."""

    name = "fake"

    def __init__(self, responses: list[dict[str, Any]], fail_at: int, exc: Exception) -> None:
        self.inner = FakeProvider(responses)
        self.fail_at = fail_at
        self.exc = exc
        self.calls = 0

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.calls - 1 == self.fail_at:
            raise self.exc
        return self.inner.complete(request)
