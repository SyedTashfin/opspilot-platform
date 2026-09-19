"""Model price table.

Prices are configuration, not truth: they are published per million tokens, they change, and they
differ by provider region. Two rules follow:

1. A model with no entry yields ``cost_known=False`` rather than a plausible-looking zero, so the
   platform never reports a cost it did not compute.
2. The table is dated and lives in one place instead of being smeared across call sites.

Figures are EUR per 1M tokens, recorded on the date in ``AS_OF``, and must be re-checked before any
cost number is shown to a user.
"""

from __future__ import annotations

from dataclasses import dataclass

from opspilot.gateway.types import Usage


@dataclass(frozen=True, slots=True)
class Price:
    input_per_million_eur: float
    output_per_million_eur: float


AS_OF = "2026-09-19"

PRICES: dict[str, Price] = {
    # Deterministic test provider: always free.
    "fake": Price(0.0, 0.0),
    "fake/fake-1": Price(0.0, 0.0),
    # Cheap models used for development and evaluation by default.
    "deepseek/deepseek-chat": Price(0.25, 1.00),
    "mistral/mistral-small-latest": Price(0.18, 0.55),
    # Mid-tier, only for the final diagnosis step when explicitly selected.
    "openai/gpt-4o-mini": Price(0.14, 0.55),
}


def lookup_price(model: str) -> Price | None:
    """Exact match first, then a single unambiguous prefix match.

    Providers often append a version suffix (``deepseek-chat-2026-05``); resolving by prefix stops a
    version bump from silently making every call uncosted, while an ambiguous prefix still returns
    ``None`` rather than guessing.
    """
    if model in PRICES:
        return PRICES[model]
    candidates = [key for key in PRICES if model.startswith(f"{key}-")]
    if len(candidates) == 1:
        return PRICES[candidates[0]]
    return None


def cost_eur(model: str, usage: Usage) -> tuple[float | None, bool]:
    """Return ``(cost, known)``. Unknown pricing yields ``(None, False)`` by design."""
    price = lookup_price(model)
    if price is None:
        return None, False
    amount = (
        usage.input_tokens * price.input_per_million_eur + usage.output_tokens * price.output_per_million_eur
    ) / 1_000_000
    return round(amount, 6), True
