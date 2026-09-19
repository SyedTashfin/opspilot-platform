"""Model gateway: the only path from the platform to a language model.

Agents never import a vendor SDK. They call this package, which owns model policy, fallback,
retries, timeouts, budget enforcement and per-call cost accounting.
"""

from opspilot.gateway.errors import (
    AllModelsFailed,
    BudgetExceeded,
    GatewayError,
    NoProviderAvailable,
    ProviderBadResponse,
    ProviderError,
    ProviderTimeout,
)
from opspilot.gateway.gateway import ModelGateway
from opspilot.gateway.policy import ModelChain, ModelPolicy
from opspilot.gateway.types import Message, ModelRequest, ModelResponse, ProviderResult, Usage

__all__ = [
    "AllModelsFailed",
    "BudgetExceeded",
    "GatewayError",
    "Message",
    "ModelChain",
    "ModelGateway",
    "ModelPolicy",
    "ModelRequest",
    "ModelResponse",
    "NoProviderAvailable",
    "ProviderBadResponse",
    "ProviderError",
    "ProviderResult",
    "ProviderTimeout",
    "Usage",
]
