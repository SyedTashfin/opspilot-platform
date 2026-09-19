"""Telemetry value types, shaped like the questions an investigator actually asks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

SourceLabel = Literal["demo", "live"]


@dataclass(frozen=True, slots=True)
class MetricPoint:
    at: datetime
    value: float


@dataclass(frozen=True, slots=True)
class MetricSeries:
    name: str
    unit: str
    points: tuple[MetricPoint, ...]

    @property
    def latest(self) -> float:
        return self.points[-1].value if self.points else 0.0

    @property
    def maximum(self) -> float:
        return max((point.value for point in self.points), default=0.0)

    def percentile(self, fraction: float) -> float:
        if not self.points:
            return 0.0
        values = sorted(point.value for point in self.points)
        index = min(len(values) - 1, max(0, round(fraction * (len(values) - 1))))
        return values[index]

    def timedelta_values(self) -> tuple[float, ...]:
        return tuple(point.value for point in self.points)


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    service: str
    window_minutes: int
    series: tuple[MetricSeries, ...]
    source: SourceLabel

    def named(self, name: str) -> MetricSeries | None:
        for series in self.series:
            if series.name == name:
                return series
        return None

    def as_observation(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "window_minutes": self.window_minutes,
            "source": self.source,
            "series": {
                series.name: {
                    "unit": series.unit,
                    "latest": series.latest,
                    "max": series.maximum,
                    "p95": series.percentile(0.95),
                }
                for series in self.series
            },
        }


@dataclass(frozen=True, slots=True)
class LogLine:
    at: datetime
    service: str
    level: str
    message: str
    attributes: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Deployment:
    service: str
    version: str
    deployed_at: datetime
    author: str
    changes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResourceState:
    service: str
    replicas: int
    desired_replicas: int
    cpu_limit: str
    memory_limit: str
    restarts_last_hour: int
    source: SourceLabel
