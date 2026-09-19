"""Model price table.

Prices are configuration, not truth: they are published per million tokens, they change, and they
differ by provider region and even by time of day. Three rules follow:

1. A model with no entry yields ``cost_known=False`` rather than a plausible-looking zero, so the
   platform never reports a cost it did not compute.
2. The table is dated and lives in one place instead of being smeared across call sites.
3. Where a provider charges by time of day, the window is encoded rather than averaged away: a cost
   that is silently 2x wrong is worse than one that names its window.

Source for the DeepSeek rows (retrieved 2026-09-19 from the provider's own pricing page,
``api-docs.deepseek.com/quick_start/pricing``):

* ``deepseek-flash``: cache-miss input $0.15, output $0.60 per 1M tokens, off-peak; cache-hit input
  $0.003. ``deepseek-v4-pro``: $0.66 / $1.98 off-peak, $0.022 cache-hit input.
* Off-peak rates are **half** peak rates. Peak hours are 01:00-04:00 and 06:00-10:00 UTC, Monday to
  Friday; every other hour is off-peak.
* The legacy names ``deepseek-chat`` and ``deepseek-reasoner`` were retired on 2026-07-24 and now route
  to the Flash model, billed at the Flash price. Table entries for them remain so an older configuration
  is priced rather than silently uncosted.

Two stated limitations, because a cost claim should carry them:

* All input is billed at the **cache-miss** rate. The platform's ``Usage`` cannot distinguish cached
  input, so the estimate is conservative — it can overstate, never understate.
* Prices are published in USD; the table is in EUR at a fixed rate (``USD_TO_EUR``). That assumption is
  recorded on ``AS_OF`` rather than hidden in a conversion nobody can see.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from opspilot.gateway.types import Usage


@dataclass(frozen=True, slots=True)
class Price:
    """Off-peak rates in EUR per 1M tokens. ``peak_multiplier`` applies inside the provider's windows."""

    input_per_million_eur: float
    output_per_million_eur: float
    peak_multiplier: float = 1.0
    windowed: bool = False

    def input_rate(self, *, peak: bool) -> float:
        return self.input_per_million_eur * (self.peak_multiplier if peak and self.windowed else 1.0)

    def output_rate(self, *, peak: bool) -> float:
        return self.output_per_million_eur * (self.peak_multiplier if peak and self.windowed else 1.0)


AS_OF = "2026-09-19"
USD_TO_EUR = 0.92

#: DeepSeek peak windows in UTC, as half-open (start_hour, end_hour) pairs, Monday to Friday.
PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))


def _eur(usd: float) -> float:
    return round(usd * USD_TO_EUR, 6)


def is_peak(at: dt.datetime) -> bool:
    """Whether ``at`` falls inside the provider's peak window. Weekends are entirely off-peak."""
    moment = at.astimezone(dt.UTC)
    if moment.weekday() >= 5:
        return False
    return any(start <= moment.hour < end for start, end in PEAK_WINDOWS_UTC)


PRICES: dict[str, Price] = {
    # Deterministic test provider: always free.
    "fake": Price(0.0, 0.0),
    "fake/fake-1": Price(0.0, 0.0),
    # DeepSeek, off-peak rates; peak doubles them.
    "deepseek-flash": Price(_eur(0.15), _eur(0.60), peak_multiplier=2.0, windowed=True),
    "deepseek/deepseek-flash": Price(_eur(0.15), _eur(0.60), peak_multiplier=2.0, windowed=True),
    "deepseek-v4-pro": Price(_eur(0.66), _eur(1.98), peak_multiplier=2.0, windowed=True),
    "deepseek/deepseek-v4-pro": Price(_eur(0.66), _eur(1.98), peak_multiplier=2.0, windowed=True),
    # Retired names, still routed by the provider and billed at the Flash price.
    "deepseek-chat": Price(_eur(0.15), _eur(0.60), peak_multiplier=2.0, windowed=True),
    "deepseek/deepseek-chat": Price(_eur(0.15), _eur(0.60), peak_multiplier=2.0, windowed=True),
    "deepseek-reasoner": Price(_eur(0.15), _eur(0.60), peak_multiplier=2.0, windowed=True),
    "deepseek/deepseek-reasoner": Price(_eur(0.15), _eur(0.60), peak_multiplier=2.0, windowed=True),
    # Other providers: flat published rates, no time-of-day pricing.
    "mistral/mistral-small-latest": Price(0.18, 0.55),
    "openai/gpt-4o-mini": Price(0.14, 0.55),
}


def lookup_price(model: str) -> Price | None:
    """Exact match first, then a single unambiguous prefix match.

    Providers often append a version suffix (``deepseek-flash-2026-09``); resolving by prefix stops a
    version bump from silently making every call uncosted, while an ambiguous prefix still returns
    ``None`` rather than guessing.
    """
    if model in PRICES:
        return PRICES[model]
    candidates = [key for key in PRICES if model.startswith(f"{key}-")]
    if len(candidates) == 1:
        return PRICES[candidates[0]]
    return None


def cost_eur(model: str, usage: Usage, *, at: dt.datetime | None = None) -> tuple[float | None, bool]:
    """Return ``(cost, known)``. Unknown pricing yields ``(None, False)`` by design.

    ``at`` decides which rate window applies; it defaults to now, and callers that know when the call
    happened should pass it rather than letting the cost depend on when it was computed.
    """
    price = lookup_price(model)
    if price is None:
        return None, False
    peak = price.windowed and is_peak(at or dt.datetime.now(dt.UTC))
    amount = (
        usage.input_tokens * price.input_rate(peak=peak) + usage.output_tokens * price.output_rate(peak=peak)
    ) / 1_000_000
    return round(amount, 6), True


def rate_window(model: str, *, at: dt.datetime | None = None) -> str:
    """Which window applied: ``peak``, ``off-peak``, ``flat`` or ``unknown``. For display next to a cost."""
    price = lookup_price(model)
    if price is None:
        return "unknown"
    if not price.windowed:
        return "flat"
    return "peak" if is_peak(at or dt.datetime.now(dt.UTC)) else "off-peak"
