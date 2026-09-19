from __future__ import annotations

from opspilot.gateway.pricing import AS_OF, cost_eur, lookup_price
from opspilot.gateway.types import Usage


def test_exact_match_is_priced() -> None:
    price = lookup_price("deepseek/deepseek-chat")
    assert price is not None
    assert price.input_per_million_eur > 0
    assert price.output_per_million_eur > 0


def test_versioned_model_resolves_by_prefix() -> None:
    assert lookup_price("deepseek/deepseek-chat-2026-05") is not None


def test_unpriced_model_returns_none() -> None:
    assert lookup_price("exotic/model") is None


def test_cost_is_unknown_rather_than_zero_when_unpriced() -> None:
    amount, known = cost_eur("exotic/model", Usage(input_tokens=10_000, output_tokens=10_000))
    assert amount is None
    assert known is False


def test_cost_math_per_million_tokens() -> None:
    amount, known = cost_eur("deepseek/deepseek-chat", Usage(input_tokens=1_000_000, output_tokens=1_000_000))
    assert known is True
    assert amount == 1.25  # 0.25 input + 1.00 output


def test_zero_priced_model_is_known_not_unknown() -> None:
    amount, known = cost_eur("fake/fake-1", Usage(input_tokens=500, output_tokens=500))
    assert known is True
    assert amount == 0.0


def test_price_table_carries_its_date() -> None:
    # Prices change; the date is what tells a reader whether to trust the numbers.
    assert AS_OF == "2026-09-19"
