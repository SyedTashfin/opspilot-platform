"""The provider contract.

A provider is the only component allowed to know a vendor's wire format. It answers two questions:
"do you serve this model?" and "here is the completion". Everything else — retries, fallback,
budget, accounting — belongs to the gateway.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from opspilot.gateway.types import ModelRequest, ProviderResult


@runtime_checkable
class ModelProvider(Protocol):
    name: str

    def supports(self, model: str) -> bool:
        """Whether this provider can serve ``model`` (typically by prefix)."""
        ...

    async def complete(self, model: str, request: ModelRequest) -> ProviderResult:
        """Perform one completion.

        Raises ``ProviderTimeout``, ``ProviderError`` or ``ProviderBadResponse`` — never a bare
        ``Exception`` — so the gateway can decide between retrying, falling back and failing.
        """
        ...
