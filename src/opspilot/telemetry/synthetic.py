"""Deterministic synthetic telemetry.

Same scenario, same numbers, every run — which is what makes an evaluation suite meaningful. The
generator is seeded from the scenario id and metric name, so a test can assert exact values without
freezing the clock, and two runs of the same scenario can be compared line by line.

This source is explicitly labelled ``demo``. The agent sees that label, the UI shows it, and the
evaluation report inherits it: no measured claim can be traced back to this file.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from opspilot.telemetry.scenarios import IncidentScenario, MetricProfile
from opspilot.telemetry.types import (
    Deployment,
    LogLine,
    MetricPoint,
    MetricSeries,
    MetricsSnapshot,
    ResourceState,
    SourceLabel,
)

BASE_TIMESTAMP = datetime(2026, 6, 26, 14, 0, tzinfo=UTC)


@dataclass
class SyntheticTelemetrySource:
    scenario: IncidentScenario
    label: SourceLabel = "demo"

    def _minute(self, minute: int) -> datetime:
        return BASE_TIMESTAMP + timedelta(minutes=minute)

    def _series(self, profile: MetricProfile) -> MetricSeries:
        # Seeded PRNG: this produces the *noise* in synthetic telemetry and must be reproducible,
        # which is the opposite of what S311 guards against. Security-irrelevant by construction.
        rng = random.Random(  # noqa: S311
            f"{self.scenario.scenario_id}:{profile.name}"
        )
        points: list[MetricPoint] = []
        for minute in range(self.scenario.window_minutes):
            value = profile.incident if minute >= profile.incident_start_minute else profile.baseline
            if profile.noise:
                value += rng.uniform(-profile.noise, profile.noise)
            points.append(MetricPoint(at=self._minute(minute), value=round(value, 3)))
        return MetricSeries(name=profile.name, unit=profile.unit, points=tuple(points))

    async def metrics(self, service: str, window_minutes: int) -> MetricsSnapshot:
        self._assert_service(service)
        profiles = tuple(
            MetricProfile(
                name=profile.name,
                unit=profile.unit,
                baseline=profile.baseline,
                incident=profile.incident,
                incident_start_minute=profile.incident_start_minute,
                noise=profile.noise,
            )
            for profile in self.scenario.metrics
        )
        series = tuple(self._series(profile) for profile in profiles)
        return MetricsSnapshot(
            service=service,
            window_minutes=min(window_minutes, self.scenario.window_minutes),
            series=series,
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
        self._assert_service(service)
        cutoff = self.scenario.window_minutes - window_minutes
        lines = [
            LogLine(
                at=self._minute(entry.minute),
                service=service,
                level=entry.level,
                message=entry.message,
                attributes=dict(entry.attributes),
            )
            for entry in self.scenario.logs
            if entry.minute >= max(0, cutoff)
        ]
        if level is not None:
            wanted = level.upper()
            lines = [line for line in lines if line.level == wanted]
        if contains is not None:
            needle = contains.lower()
            lines = [line for line in lines if needle in line.message.lower()]
        return lines[:limit]

    async def deployments(self, service: str, *, limit: int = 5) -> list[Deployment]:
        self._assert_service(service)
        deployments = [
            Deployment(
                service=service,
                version=entry.version,
                deployed_at=self._minute(entry.minute),
                author=entry.author,
                changes=entry.changes,
            )
            for entry in self.scenario.deployments
        ]
        deployments.sort(key=lambda deployment: deployment.deployed_at, reverse=True)
        return deployments[:limit]

    async def resource_state(self, service: str) -> ResourceState:
        self._assert_service(service)
        return ResourceState(
            service=service,
            replicas=self.scenario.replicas,
            desired_replicas=self.scenario.desired_replicas,
            cpu_limit=self.scenario.cpu_limit,
            memory_limit=self.scenario.memory_limit,
            restarts_last_hour=self.scenario.restarts_last_hour,
            source=self.label,
        )

    def _assert_service(self, service: str) -> None:
        if service != self.scenario.service:
            msg = (
                f"scenario {self.scenario.scenario_id!r} only describes "
                f"{self.scenario.service!r}, not {service!r}"
            )
            raise KeyError(msg)
