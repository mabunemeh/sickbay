"""The agent: a tool-calling loop over a HostView, diagnose-only (no write tools).

Per turn: check the budget, call the model with the full append-only history, then
- tool_use: append the assistant content EXACTLY as returned (thinking blocks included),
  run every tool_use block through the Recorder in block order, and append ONE user message
  holding all the tool_result blocks;
- end_turn: the last text block is the JSON diagnosis;
- refusal / max_tokens / anything else: the case ends with that outcome.
After MAX_TURNS model calls without end_turn the case is invalid_output ("turn_limit").
"""

from __future__ import annotations

from importlib import resources
from typing import Any

from sickbay.agent.pricing import Budget, price_for
from sickbay.agent.providers.base import Provider
from sickbay.agent.request import Meter, build_request, call_or_abort, final_outcome
from sickbay.agent.tools import api_tools
from sickbay.evals.predictor import Prediction
from sickbay.evals.trace import Recorder
from sickbay.snapshot.hostview import HostView
from sickbay.tools import serialize

PROMPT_VERSION = "agent-v1"
MAX_TURNS = 25
_SYSTEM_HEADER = "[system]\n"
_USER_HEADER = "\n[user]\n"


def load_prompt() -> tuple[str, str]:
    """(system prompt, AGENT_TASK) from the packaged prompts/agent_v1.txt."""
    text = (
        resources.files("sickbay.agent")
        .joinpath("prompts", "agent_v1.txt")
        .read_bytes()
        .decode("utf-8")
    )
    if not text.startswith(_SYSTEM_HEADER) or text.count(_USER_HEADER) != 1:
        raise ValueError("agent_v1.txt must be '[system]' text then '[user]' text")
    system, task = text[len(_SYSTEM_HEADER) :].split(_USER_HEADER)
    return system, task


def tool_result_block(tool_use_id: Any, call_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """The result the model reads; an error result is data, so is_error stays false."""
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": serialize({"call_id": call_id, "result": result}),
        "is_error": False,
    }


class AgentPredictor:
    name = "agent"
    prompt_version: str | None = PROMPT_VERSION

    def __init__(self, provider: Provider, model: str, effort: str, budget: Budget) -> None:
        price_for(model)  # UnknownPrice before any call
        self.provider = provider
        self.model: str | None = model
        self.effort: str | None = effort
        self.budget = budget
        self._model = model
        self._effort = effort
        self._system, self._task = load_prompt()
        self._tools = api_tools()

    def predict(self, view: HostView) -> Prediction:
        recorder = Recorder(view)
        meter = Meter(self.provider, self._model, self.budget)
        messages: list[dict[str, Any]] = [{"role": "user", "content": self._task}]
        for _ in range(MAX_TURNS):
            request = build_request(self._model, self._effort, self._system, messages, self._tools)
            response = call_or_abort(meter, request, recorder.trace)
            if response.get("stop_reason") != "tool_use":
                outcome, diagnosis, detail = final_outcome(response)
                return meter.prediction(outcome, diagnosis, recorder.trace, detail)
            content = response.get("content")
            blocks = content if isinstance(content, list) else []
            messages.append({"role": "assistant", "content": content})
            results = []
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    call_id, result = recorder.call(block.get("name"), block.get("input"))
                    results.append(tool_result_block(block.get("id"), call_id, result))
            if not results:  # an empty user message would be rejected by the API
                detail = "stop_reason tool_use without a tool_use block"
                return meter.prediction("error", None, recorder.trace, detail)
            messages.append({"role": "user", "content": results})
        return meter.prediction("invalid_output", None, recorder.trace, "turn_limit")
