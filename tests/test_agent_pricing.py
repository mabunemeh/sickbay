"""Prices, the Haiku long-prompt rate, and the budget. Expected values are hand-computed."""

from __future__ import annotations

import pytest

from sickbay.agent.pricing import (
    PRICES,
    PRICES_AS_OF,
    Budget,
    BudgetExceeded,
    UnknownPrice,
    call_cost,
    price_for,
)
from sickbay.agent.request import DEFAULT_EFFORT


def test_price_table() -> None:
    assert PRICES_AS_OF == "2026-11-11"
    assert set(PRICES) == {
        "claude-fable-5-2",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
    }
    assert set(DEFAULT_EFFORT) == set(PRICES)
    assert DEFAULT_EFFORT == {
        "claude-haiku-5-5": "medium",
        "claude-sonnet-5-5": "high",
        "claude-opus-5-5": "medium",
        "claude-fable-5-2": "high",
    }


def test_sonnet_cost_hand_computed() -> None:
    # 1000 x 2 + 500 x 10 + 2000 x 2.50 + 10000 x 0.10 = 13000 micro-dollars
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_creation_input_tokens": 2000,
        "cache_read_input_tokens": 10000,
    }
    assert call_cost("claude-sonnet-5-5", usage) == pytest.approx(0.013, rel=1e-12)


def test_fable_and_opus_cost_hand_computed() -> None:
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert call_cost("claude-fable-5-2", usage) == pytest.approx(48.0, rel=1e-12)
    usage = {"cache_creation_input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
    assert call_cost("claude-opus-5-5", usage) == pytest.approx(5.2, rel=1e-12)


def test_missing_and_null_counts_are_zero() -> None:
    usage = {"input_tokens": 1000, "cache_creation_input_tokens": None}
    assert call_cost("claude-sonnet-5-5", usage) == pytest.approx(0.002, rel=1e-12)


def test_haiku_at_100k_prompt_tokens_is_the_base_rate() -> None:
    # 100000 x 0.10 + 1000 x 0.50 = 10500 micro-dollars; 100,000 is not ABOVE the boundary.
    usage = {"input_tokens": 100_000, "output_tokens": 1000}
    assert call_cost("claude-haiku-5-5", usage) == pytest.approx(0.0105, rel=1e-12)
    # split over input, cache write and cache read: 5000 + 3125 + 250 = 8375
    usage = {
        "input_tokens": 50_000,
        "cache_creation_input_tokens": 25_000,
        "cache_read_input_tokens": 25_000,
    }
    assert call_cost("claude-haiku-5-5", usage) == pytest.approx(0.008375, rel=1e-12)


def test_haiku_above_100k_prompt_tokens_is_5x() -> None:
    # (100001 x 0.10 + 1000 x 0.50) x 5 = 52500.5 micro-dollars
    usage = {"input_tokens": 100_001, "output_tokens": 1000}
    assert call_cost("claude-haiku-5-5", usage) == pytest.approx(0.0525005, rel=1e-12)
    # cache tokens count toward the prompt: (5000 + 3125 + 250.01) x 5 = 41875.05
    usage = {
        "input_tokens": 50_000,
        "cache_creation_input_tokens": 25_000,
        "cache_read_input_tokens": 25_001,
    }
    assert call_cost("claude-haiku-5-5", usage) == pytest.approx(0.04187505, rel=1e-12)


def test_output_tokens_do_not_count_toward_the_haiku_boundary() -> None:
    usage = {"input_tokens": 100_000, "output_tokens": 50_000}
    # 10000 + 25000 = 35000, not multiplied
    assert call_cost("claude-haiku-5-5", usage) == pytest.approx(0.035, rel=1e-12)


def test_sonnet_has_no_long_prompt_rate() -> None:
    assert call_cost("claude-sonnet-5-5", {"input_tokens": 200_000}) == pytest.approx(0.4)


def test_unknown_model() -> None:
    with pytest.raises(UnknownPrice, match="^no price for claude-x$"):
        price_for("claude-x")
    with pytest.raises(UnknownPrice):
        call_cost("claude-x", {})


def test_budget_raises_at_or_above_the_cap() -> None:
    budget = Budget(1.0)
    budget.check()
    budget.add(0.6)
    budget.check()
    budget.add(0.4)
    with pytest.raises(BudgetExceeded):
        budget.check()  # spent == max_usd


def test_unlimited_budget_never_raises() -> None:
    budget = Budget(None)
    budget.add(1e9)
    budget.check()
    assert budget.spent == 1e9


def test_fable_price_row() -> None:
    # 1000 in x 8 + 1000 out x 40 + 1000 write x 10 + 1000 read x 0.20 = 58200 micro-dollars
    usage = {
        "input_tokens": 1000,
        "output_tokens": 1000,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 1000,
    }
    assert call_cost("claude-fable-5-2", usage) == pytest.approx(0.0582, rel=1e-12)
    # no long-prompt rate on fable
    assert call_cost("claude-fable-5-2", {"input_tokens": 200_000}) == pytest.approx(1.6, rel=1e-12)
