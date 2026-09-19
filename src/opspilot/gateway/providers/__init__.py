"""Provider implementations. Each one adapts a vendor SDK to the gateway's own types."""

from opspilot.gateway.providers.base import ModelProvider
from opspilot.gateway.providers.fake import FakeProvider

__all__ = ["FakeProvider", "ModelProvider"]
