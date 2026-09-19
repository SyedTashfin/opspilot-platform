"""Gateway error taxonomy.

Every failure mode the platform must survive has a named exception, so callers can react
deliberately instead of catching ``Exception``.
"""

from __future__ import annotations


class GatewayError(Exception):
    """Base class for gateway failures."""


class ProviderError(GatewayError):
    """A provider failed in a way that may be transient (network, rate limit, 5xx)."""


class ProviderTimeout(ProviderError):
    """The provider exceeded the configured timeout."""


class ProviderBadResponse(ProviderError):
    """The provider answered, but not in a shape we can use (including schema violations)."""


class ProviderRequestRejected(ProviderError):
    """The provider refused the request (auth, quota, bad parameters). Retrying is pointless."""


class NoProviderAvailable(GatewayError):
    """No configured provider claims the requested model."""


class AllModelsFailed(GatewayError):
    """Every model in the policy chain failed after retries."""


class BudgetExceeded(GatewayError):
    """The run or daily cost ceiling is already reached; the call was not attempted."""

    def __init__(self, scope: str, limit_eur: float, spent_eur: float) -> None:
        self.scope = scope
        self.limit_eur = limit_eur
        self.spent_eur = spent_eur
        super().__init__(
            f"{scope} budget exceeded: spent {spent_eur:.4f} EUR of {limit_eur:.4f} EUR"
        )
