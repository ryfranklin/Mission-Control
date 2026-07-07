"""Model → price table. THE single source of pricing truth for the runtime.

Prices are USD per 1,000,000 tokens, verified against the Claude models/pricing
docs on 2026-07-07. Cache multipliers are relative to the model's base *input*
price (standard Anthropic prompt-caching rates): a cache read is ~0.1x input, a
5-minute (default ephemeral) cache write is 1.25x, a 1-hour write is 2x.

If a metaphor swap or a new model lands, this is the one file to touch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Cache pricing multipliers, relative to base input price.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens."""

    input_per_mtok: float
    output_per_mtok: float


# Keyed by dateless alias. Dated snapshot IDs (e.g. ``claude-haiku-4-5-20251001``)
# resolve to their alias via :func:`resolve`.
PRICES: dict[str, ModelPrice] = {
    "claude-fable-5": ModelPrice(10.0, 50.0),
    "claude-mythos-5": ModelPrice(10.0, 50.0),
    "claude-opus-4-8": ModelPrice(5.0, 25.0),
    "claude-opus-4-7": ModelPrice(5.0, 25.0),
    "claude-opus-4-6": ModelPrice(5.0, 25.0),
    "claude-opus-4-5": ModelPrice(5.0, 25.0),
    "claude-opus-4-1": ModelPrice(15.0, 75.0),
    "claude-sonnet-5": ModelPrice(3.0, 15.0),
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0),
    "claude-sonnet-4-5": ModelPrice(3.0, 15.0),
    "claude-haiku-4-5": ModelPrice(1.0, 5.0),
}

_DATE_SUFFIX = re.compile(r"-\d{8}$")


class UnknownModelError(KeyError):
    """No price is known for a model — fail loud rather than bill $0 silently."""


def resolve(model: str) -> str:
    """Map a possibly date-suffixed model id to its priced alias."""
    if model in PRICES:
        return model
    stripped = _DATE_SUFFIX.sub("", model)
    if stripped in PRICES:
        return stripped
    raise UnknownModelError(model)


def price_for(model: str) -> ModelPrice:
    return PRICES[resolve(model)]


def cost_usd(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_5m_tokens: int = 0,
    cache_creation_1h_tokens: int = 0,
) -> float:
    """Cost of one model request, in USD, from a token breakdown."""
    p = price_for(model)
    return (
        input_tokens * p.input_per_mtok
        + output_tokens * p.output_per_mtok
        + cache_read_tokens * p.input_per_mtok * CACHE_READ_MULTIPLIER
        + cache_creation_5m_tokens * p.input_per_mtok * CACHE_WRITE_5M_MULTIPLIER
        + cache_creation_1h_tokens * p.input_per_mtok * CACHE_WRITE_1H_MULTIPLIER
    ) / 1_000_000
