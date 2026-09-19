"""The telemetry contract.

Tools depend on this protocol, never on a concrete backend. Two implementations satisfy it: an
in-process synthetic source (deterministic, used by tests and CI) and an HTTP source that reads a
running service (the Incident Lab's demo service). The agent's tools do not change between them — which
was the point of building the tools as factories over an injected source.
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


class TelemetryUnavailableError(RuntimeError):
    """The telemetry backend could not be read.

    Raised rather than returning empty results: "no data" and "we could not see the service" are
    different findings, and only one of them belongs in a diagnosis.
    """


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
