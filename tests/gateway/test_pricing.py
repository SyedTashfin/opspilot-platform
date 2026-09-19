from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opspilot.gateway.pricing import AS_OF, USD_TO_EUR, cost_eur, is_peak, lookup_price, rate_window
from opspilot.gateway.types import Usage

MILLION = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
# 2026-09-19 is a Saturday, 2026-09-21 a Monday.
SATURDAY_NOON = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
MONDAY_PEAK = datetime(2026, 9, 21, 7, 30, tzinfo=UTC)
MONDAY_OFF_PEAK = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def test_exact_match_is_priced() -> None:
    price = lookup_price("deepseek/deepseek-flash")

    assert price is not None
    assert price.input_per_million_eur > 0
    assert price.output_per_million_eur > 0
    assert price.windowed is True


def test_versioned_model_resolves_by_prefix() -> None:
    assert lookup_price("deepseek-flash-2026-09") is not None


def test_the_retired_deepseek_names_are_still_priced() -> None:
    """`deepseek-chat` was retired on 2026-07-24 and routes to Flash; it must not become uncosted."""
    legacy = lookup_price("deepseek/deepseek-chat")
    current = lookup_price("deepseek/deepseek-flash")

    assert legacy is not None and current is not None
    assert legacy.input_per_million_eur == current.input_per_million_eur


def test_unpriced_model_returns_none() -> None:
    assert lookup_price("exotic/model") is None


def test_cost_is_unknown_rather_than_zero_when_unpriced() -> None:
    amount, known = cost_eur("exotic/model", Usage(input_tokens=10_000, output_tokens=10_000))

    assert amount is None
    assert known is False


def test_cost_math_per_million_tokens_off_peak() -> None:
    amount, known = cost_eur("deepseek/deepseek-flash", MILLION, at=SATURDAY_NOON)

    assert known is True
    # cache-miss input $0.15 + output $0.60 = $0.75 per 1M+1M tokens, at 0.92 EUR/USD.
    assert amount == pytest.approx(0.69, abs=0.001)


def test_the_peak_window_doubles_the_cost() -> None:
    peak, _ = cost_eur("deepseek/deepseek-flash", MILLION, at=MONDAY_PEAK)
    off_peak, _ = cost_eur("deepseek/deepseek-flash", MILLION, at=MONDAY_OFF_PEAK)

    assert peak == pytest.approx(off_peak * 2, abs=0.001)  # type: ignore[operator]
    assert peak == pytest.approx(1.38, abs=0.001)


def test_the_provider_windows_are_encoded_not_averaged_away() -> None:
    assert is_peak(MONDAY_PEAK) is True
    assert is_peak(MONDAY_OFF_PEAK) is False
    assert is_peak(SATURDAY_NOON) is False
    assert is_peak(datetime(2026, 9, 21, 4, 30, tzinfo=UTC)) is False  # between the two windows


def test_the_rate_window_is_reported_next_to_the_cost() -> None:
    assert rate_window("deepseek/deepseek-flash", at=MONDAY_PEAK) == "peak"
    assert rate_window("deepseek/deepseek-flash", at=MONDAY_OFF_PEAK) == "off-peak"
    assert rate_window("mistral/mistral-small-latest") == "flat"
    assert rate_window("exotic/model") == "unknown"


def test_input_is_billed_conservatively_at_the_cache_miss_rate() -> None:
    """We cannot see cache hits, so the estimate must be able to overstate, never understate."""
    price = lookup_price("deepseek/deepseek-flash")
    amount, _ = cost_eur(
        "deepseek/deepseek-flash", Usage(input_tokens=1_000_000, output_tokens=0), at=SATURDAY_NOON
    )

    assert price is not None
    assert amount == pytest.approx(price.input_per_million_eur, abs=0.001)


def test_zero_priced_model_is_known_not_unknown() -> None:
    amount, known = cost_eur("fake/fake-1", Usage(input_tokens=500, output_tokens=500))

    assert known is True
    assert amount == 0.0


def test_price_table_carries_its_date() -> None:
    # Prices change; the date is what tells a reader whether to trust the numbers.
    assert AS_OF == "2026-09-19"


def test_the_currency_assumption_is_recorded_not_hidden() -> None:
    assert 0.5 < USD_TO_EUR < 1.5
