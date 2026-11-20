"""Model prices (USD per million tokens), the per-call cost and the run's spending cap.

Prices are from the provider's pricing page as of PRICES_AS_OF (plan, batch 0 section d).
The cap is checked BEFORE each model call, so a run can overshoot it by at most one call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PRICES_AS_OF = "2026-11-11"

# Haiku 5.5 bills every rate at 5x when a call's prompt (input + cache write + cache read)
# is above this many tokens.
HAIKU_LONG_PROMPT_TOKENS = 100_000
HAIKU_LONG_PROMPT_FACTOR = 5


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_write: float  # 5-minute cache write
    cache_read: float
    long_prompt_tokens: int | None = None  # above this, every rate is multiplied
    long_prompt_factor: int = 1


PRICES: dict[str, Price] = {
    "claude-fable-5-2": Price(8, 40, 10, 0.20),
    "claude-opus-5-5": Price(4, 20, 5, 0.20),
    "claude-sonnet-5-5": Price(2, 10, 2.50, 0.10),
    "claude-haiku-5-5": Price(
        0.10,
        0.50,
        0.125,
        0.01,
        long_prompt_tokens=HAIKU_LONG_PROMPT_TOKENS,
        long_prompt_factor=HAIKU_LONG_PROMPT_FACTOR,
    ),
}


class UnknownPrice(ValueError):
    """No price is known for this model id."""


class BudgetExceeded(Exception):
    """The run's spending cap was reached before a model call."""


def price_for(model: str) -> Price:
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownPrice(f"no price for {model}") from None


def _tokens(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def call_cost(model: str, usage: dict[str, Any]) -> float:
    """USD for one call, from the response's `usage` object (missing counts are 0)."""
    price = price_for(model)
    tokens_in = _tokens(usage, "input_tokens")
    tokens_out = _tokens(usage, "output_tokens")
    write = _tokens(usage, "cache_creation_input_tokens")
    read = _tokens(usage, "cache_read_input_tokens")
    micro = (
        tokens_in * price.input
        + tokens_out * price.output
        + write * price.cache_write
        + read * price.cache_read
    )
    prompt = tokens_in + write + read
    if price.long_prompt_tokens is not None and prompt > price.long_prompt_tokens:
        micro *= price.long_prompt_factor
    return micro / 1_000_000


class Budget:
    """One spending cap shared by every case and repeat of a run.

    check() raises BudgetExceeded when spent >= max_usd; it is called before each model
    call, so the overshoot is at most the cost of one call. max_usd None means unlimited
    (replay).
    """

    def __init__(self, max_usd: float | None) -> None:
        self.max_usd = max_usd
        self.spent = 0.0

    def check(self) -> None:
        if self.max_usd is not None and self.spent >= self.max_usd:
            raise BudgetExceeded(f"spent ${self.spent:.6f} of the ${self.max_usd} cap")

    def add(self, usd: float) -> None:
        self.spent += usd
