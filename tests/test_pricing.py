"""Pricing table + cost math (the one pricing module)."""

from __future__ import annotations

import pytest

from mission_control import pricing


def test_resolves_dated_snapshot_to_alias():
    assert pricing.resolve("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert pricing.resolve("claude-haiku-4-5") == "claude-haiku-4-5"


def test_unknown_model_fails_loud():
    with pytest.raises(pricing.UnknownModelError):
        pricing.cost_usd("gpt-4", input_tokens=100)


def test_cost_includes_output_and_cache_multipliers():
    # Haiku 4.5: $1 in / $5 out per MTok; cache read 0.1x, 5m write 1.25x.
    cost = pricing.cost_usd(
        "claude-haiku-4-5-20251001",
        input_tokens=1200,
        output_tokens=340,
        cache_read_tokens=800,
        cache_creation_5m_tokens=200,
        cache_creation_1h_tokens=0,
    )
    expected = (1200 * 1 + 340 * 5 + 800 * 0.1 + 200 * 1.25) / 1_000_000
    assert cost == pytest.approx(expected)


def test_one_hour_cache_write_is_pricier_than_five_minute():
    five = pricing.cost_usd("claude-opus-4-8", cache_creation_5m_tokens=1000)
    hour = pricing.cost_usd("claude-opus-4-8", cache_creation_1h_tokens=1000)
    assert hour > five
