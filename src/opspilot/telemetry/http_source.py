"""Telemetry over HTTP: the platform's view of a service that is actually running.

The lab (and, in M9, the same service deployed to Container Apps) exposes the platform's telemetry
shapes, so this adapter is thin: read, validate, translate. Two properties matter more than the code:

* **It never touches the admin surface.** Only ``/metrics``, ``/logs``, ``/deployments`` and ``/state``
  are read. A test asserts that no registered tool URL contains ``/admin``.
* **Unavailable telemetry raises.** An empty result would read as "the service is quiet" when the truth
  is "we could not see the service", and that difference decides whether an investigation is honest.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from opspilot.telemetry.source import TelemetryUnavailableError
from opspilot.telemetry.types import (
    Deployment,
    LogLine,
    MetricPoint,
    MetricSeries,
    MetricsSnapshot,
    ResourceState,
    SourceLabel,
)

READ_PATHS = ("/metrics", "/logs", "/deployments", "/state")


class HttpTelemetrySource:
    """A ``TelemetrySource`` backed by a real service over HTTP."""

    label: SourceLabel

    def __init__(
        self,
        base_url: str,
        *,
        label: SourceLabel = "demo",
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.label = label
        self._timeout = timeout_seconds
        self._transport = transport

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            msg = f"telemetry request to {path} failed: {type(exc).__name__}: {exc}"
            raise TelemetryUnavailableError(msg) from exc
        except ValueError as exc:
            msg = f"telemetry response from {path} was not JSON: {exc}"
            raise TelemetryUnavailableError(msg) from exc
        if not isinstance(payload, dict):
            msg = f"telemetry response from {path} was {type(payload).__name__}, expected an object"
            raise TelemetryUnavailableError(msg)
        return payload

    @staticmethod
    def _check_service(payload: dict[str, Any], service: str, path: str) -> None:
        reported = payload.get("service")
        if reported is not None and reported != service:
            msg = (
                f"{path} describes service {reported!r}, not {service!r}: refusing to mix telemetry "
                f"from two services"
            )
            raise TelemetryUnavailableError(msg)

    async def metrics(self, service: str, window_minutes: int) -> MetricsSnapshot:
        payload = await self._get("/metrics", {"window_minutes": window_minutes})
        self._check_service(payload, service, "/metrics")
        series = []
        for raw in payload.get("series", []):
            points = tuple(
                MetricPoint(at=datetime.fromisoformat(point["at"]), value=float(point["value"]))
                for point in raw.get("points", [])
            )
            series.append(MetricSeries(name=raw["name"], unit=raw.get("unit", ""), points=points))
        return MetricsSnapshot(
            service=payload.get("service", service),
            window_minutes=int(payload.get("window_minutes", window_minutes)),
            series=tuple(series),
            source=self.label,
        )

    async def logs(
        self,
        service: str,
        window_minutes: int,
        *,
        level: str | None = None,
        contains: str | None = None,
        limit: int = 50,
    ) -> list[LogLine]:
        params: dict[str, Any] = {"window_minutes": window_minutes, "limit": limit}
        if level is not None:
            params["level"] = level
        if contains is not None:
            params["contains"] = contains
        payload = await self._get("/logs", params)
        self._check_service(payload, service, "/logs")
        return [
            LogLine(
                at=datetime.fromisoformat(line["at"]),
                service=payload.get("service", service),
                level=line.get("level", "INFO"),
                message=line.get("message", ""),
                attributes=line.get("attributes", {}),
            )
            for line in payload.get("lines", [])
        ]

    async def deployments(self, service: str, *, limit: int = 5) -> list[Deployment]:
        payload = await self._get("/deployments", {"limit": limit})
        self._check_service(payload, service, "/deployments")
        return [
            Deployment(
                service=payload.get("service", service),
                version=raw["version"],
                deployed_at=datetime.fromisoformat(raw["at"]),
                author=raw.get("author", "unknown"),
                changes=tuple(raw.get("changes", ())),
            )
            for raw in payload.get("deployments", [])
        ]

    async def resource_state(self, service: str) -> ResourceState:
        payload = await self._get("/state")
        self._check_service(payload, service, "/state")
        return ResourceState(
            service=payload.get("service", service),
            replicas=int(payload["replicas"]),
            desired_replicas=int(payload["desired_replicas"]),
            cpu_limit=payload.get("cpu_limit", "unknown"),
            memory_limit=payload.get("memory_limit", "unknown"),
            restarts_last_hour=int(payload.get("restarts_last_hour", 0)),
            source=self.label,
        )
