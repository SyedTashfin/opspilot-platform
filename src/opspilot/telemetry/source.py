"""The telemetry contract.

Tools depend on this protocol, never on a concrete backend. Today it is satisfied by an in-process
synthetic source; the Incident Lab's demo service (M6) satisfies it over HTTP with the same methods,
so the agent's tools do not change when demo becomes "real" telemetry from a running service.
"""

from __future__ import annotations

from typing import Protocol

from opspilot.telemetry.types import (
    Deployment,
    LogLine,
    MetricsSnapshot,
    ResourceState,
    SourceLabel,
)


class TelemetrySource(Protocol):
    @property
    def label(self) -> SourceLabel:
        """``demo`` for generated data, ``live`` for a real service's telemetry."""
        ...

    async def metrics(self, service: str, window_minutes: int) -> MetricsSnapshot: ...

    async def logs(
        self,
        service: str,
        window_minutes: int,
        *,
        level: str | None = None,
        contains: str | None = None,
        limit: int = 50,
    ) -> list[LogLine]: ...

    async def deployments(self, service: str, *, limit: int = 5) -> list[Deployment]: ...

    async def resource_state(self, service: str) -> ResourceState: ...
