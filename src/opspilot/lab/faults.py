"""Incident Lab: fault model, observations and the metrics derived from them.

The lab is a real service that really misbehaves when a fault is injected, so the telemetry an
investigation reads is generated from actual request outcomes rather than from a decorative curve.
Two honest exceptions, both stated in the code where they occur:

* a healthy backlog is seeded at startup, so an investigation opened a minute after the container
  starts still has a window to look at;
* CPU, memory and dependency series are derived from the active fault, because a synthetic service has
  no real CPU to measure, and inventing a plausible number without saying so is the thing this
  project keeps refusing to do.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

BUCKET_SECONDS = 60
WINDOW_MINUTES = 30
MAX_OBSERVATIONS = 20_000


class FaultKind(StrEnum):
    HEALTHY = "healthy"
    LATENCY = "latency"
    ERRORS = "errors"
    DEPENDENCY_TIMEOUT = "dependency_timeout"
    CPU_SATURATION = "cpu_saturation"


class FaultConfig(BaseModel):
    """The injected fault *and* the answer key.

    The answer key fields (``root_cause``, ``acceptable_diagnoses``, ``expected_tools``) are served only
    by the admin API, which no agent tool points at. They exist so the evaluation suite can score a
    diagnosis without a human reading it.
    """

    kind: FaultKind = FaultKind.HEALTHY
    latency_ms: int = Field(default=0, ge=0, le=30_000)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    dependency_timeout_ms: int = Field(default=0, ge=0, le=30_000)
    dependency_retries: int = Field(default=1, ge=0, le=10)
    cpu_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    note: str = ""
    # Withheld ground truth.
    root_cause: str = ""
    acceptable_diagnoses: tuple[str, ...] = ()
    expected_tools: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return self.kind is not FaultKind.HEALTHY


class DeploymentChange(BaseModel):
    version: str
    changes: tuple[str, ...] = ()
    author: str = "deploy-bot"
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class Observation:
    """One request the service served. ``synthetic`` marks the seeded backlog."""

    at: datetime
    latency_ms: float
    status: int
    synthetic: bool = False


@dataclass
class LogLine:
    at: datetime
    level: str
    message: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class LabState:
    """Everything the lab knows about itself. One instance per running service."""

    service: str = "recommendation-service"
    scenario_id: str = "healthy"
    fault: FaultConfig = field(default_factory=FaultConfig)
    observations: list[Observation] = field(default_factory=list)
    logs: list[LogLine] = field(default_factory=list)
    deployments: list[DeploymentChange] = field(default_factory=list)
    injected_at: datetime | None = None
    restarts: int = 0
    #: Injection history, served only by the admin API. Kept out of ``logs`` on purpose: a production
    #: service does not print "a fault was injected", and an agent reading the logs must not be handed
    #: the answer key.
    injections: list[dict[str, Any]] = field(default_factory=list)

    # -- observation ------------------------------------------------------------------------
    def observe(self, *, latency_ms: float, status: int, synthetic: bool = False) -> Observation:
        observation = Observation(
            at=datetime.now(UTC), latency_ms=latency_ms, status=status, synthetic=synthetic
        )
        self.observations.append(observation)
        if len(self.observations) > MAX_OBSERVATIONS:
            del self.observations[: len(self.observations) - MAX_OBSERVATIONS]
        return observation

    def log(self, level: str, message: str, **attributes: Any) -> None:
        self.logs.append(LogLine(at=datetime.now(UTC), level=level, message=message, attributes=attributes))
        if len(self.logs) > 5_000:
            del self.logs[: len(self.logs) - 5_000]

    def seed_backlog(self, *, minutes: int = WINDOW_MINUTES, every_seconds: int = 10) -> int:
        """Healthy history, so the window is never empty in a fresh container."""
        now = datetime.now(UTC)
        count = 0
        for offset in range(minutes * 60 // every_seconds, 0, -1):
            at = now - timedelta(seconds=offset * every_seconds)
            self.observations.append(
                Observation(
                    at=at,
                    latency_ms=90.0 + (offset % 7) * 6.0,
                    status=200,
                    synthetic=True,
                )
            )
            count += 1
        return count

    # -- fault control ----------------------------------------------------------------------
    def inject(self, fault: FaultConfig, *, deployment: DeploymentChange | None = None) -> None:
        self.fault = fault
        self.injected_at = datetime.now(UTC)
        self.injections.append(
            {
                "at": self.injected_at.isoformat(),
                "kind": fault.kind.value,
                "note": fault.note,
                "latency_ms": fault.latency_ms,
                "error_rate": fault.error_rate,
            }
        )
        if deployment is not None:
            # A deployment *is* visible to the service's own telemetry: it is a real event the
            # investigation is supposed to correlate against. The timestamp is stamped at injection:
            # a scenario object is built once at import, and its "now" would otherwise be older than
            # the service's own baseline deployment, which would silently invert the timeline.
            self.deployments.append(deployment.model_copy(update={"at": datetime.now(UTC)}))
            self.log(
                "INFO",
                f"deployment {deployment.version} rolled out",
                version=deployment.version,
                changes=list(deployment.changes),
            )

    def clear(self) -> None:
        self.fault = FaultConfig()
        self.injected_at = None
        self.injections.append({"at": datetime.now(UTC).isoformat(), "kind": "cleared", "note": ""})

    def reset(self, *, baseline_version: str = "rec-2026.05.9") -> None:
        """Return the lab to a clean baseline between cases.

        Clearing the fault is not enough: deployments, logs and observations from the previous case stay
        behind, and an investigation then explains the *previous* incident with this case's alert. Every
        field a case can observe is reset here, and a live run is what showed why that matters.
        """
        self.fault = FaultConfig()
        self.injected_at = None
        self.logs.clear()
        self.deployments.clear()
        self.observations.clear()
        self.restarts = 0
        self.injections.clear()
        self.seed_backlog()
        self.deployments.append(
            DeploymentChange(
                version=baseline_version,
                changes=("dependency client timeout unchanged at 2000ms", "cache warmup off"),
            )
        )
        self.log("INFO", "service started", version=baseline_version)

    def record_restart(self) -> None:
        self.restarts += 1
        self.log("WARN", "service restarted by remediation")

    # -- derived telemetry ------------------------------------------------------------------
    def recent(self, *, minutes: int = WINDOW_MINUTES) -> list[Observation]:
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        return [observation for observation in self.observations if observation.at >= cutoff]

    def buckets(self, *, minutes: int = WINDOW_MINUTES) -> list[datetime]:
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        start = now - timedelta(minutes=minutes - 1)
        return [start + timedelta(minutes=index) for index in range(minutes)]

    def metric_series(self, *, minutes: int = WINDOW_MINUTES) -> list[dict[str, Any]]:
        """Series keyed to the bucket minute.

        Latency, request rate and error rate are computed from observed requests. CPU, memory and
        dependency series are *derived from the active fault* and labelled as such in ``basis``.
        """
        buckets = self.buckets(minutes=minutes)
        window = self.recent(minutes=minutes)
        by_bucket: dict[datetime, list[Observation]] = {bucket: [] for bucket in buckets}
        for observation in window:
            key = observation.at.replace(second=0, microsecond=0)
            if key in by_bucket:
                by_bucket[key].append(observation)

        def points(values: list[float]) -> list[dict[str, Any]]:
            return [
                {"at": bucket.isoformat(), "value": round(value, 3)}
                for bucket, value in zip(buckets, values, strict=True)
            ]

        latency_p95: list[float] = []
        request_rate: list[float] = []
        error_rate: list[float] = []
        for bucket in buckets:
            samples = by_bucket[bucket]
            if not samples:
                latency_p95.append(0.0)
                request_rate.append(0.0)
                error_rate.append(0.0)
                continue
            ordered = sorted(sample.latency_ms for sample in samples)
            rank = max(1, round(0.95 * len(ordered)))
            latency_p95.append(ordered[min(rank, len(ordered)) - 1])
            request_rate.append(len(samples) * 60 / BUCKET_SECONDS)
            failures = sum(1 for sample in samples if sample.status >= 500)
            error_rate.append(100.0 * failures / len(samples))

        fault = self.fault
        fault_start = self.injected_at.replace(second=0, microsecond=0) if self.injected_at else None

        def derived(baseline: float, faulted: float) -> list[float]:
            if fault_start is None:
                return [baseline] * len(buckets)
            return [faulted if bucket >= fault_start else baseline for bucket in buckets]

        dependency_p95 = derived(
            80.0,
            float(fault.dependency_timeout_ms) if fault.kind is FaultKind.DEPENDENCY_TIMEOUT else 80.0,
        )
        retry_rate = derived(
            0.4,
            float(fault.dependency_retries) * 3.0 if fault.kind is FaultKind.DEPENDENCY_TIMEOUT else 0.4,
        )
        cpu = derived(22.0, fault.cpu_percent or 22.0)
        memory = derived(38.0, 41.0 if fault.kind is FaultKind.CPU_SATURATION else 38.0)

        return [
            {
                "name": "request_latency_p95_ms",
                "unit": "ms",
                "basis": "nearest-rank p95 of request latencies observed in each minute",
                "points": points(latency_p95),
            },
            {
                "name": "request_rate_rps",
                "unit": "rps",
                "basis": "requests observed per minute / 60",
                "points": points(request_rate),
            },
            {
                "name": "error_rate_percent",
                "unit": "%",
                "basis": "5xx responses / responses observed in each minute",
                "points": points(error_rate),
            },
            {
                "name": "cpu_utilisation_percent",
                "unit": "%",
                "basis": "derived from the injected fault: a synthetic service has no CPU to measure",
                "points": points(cpu),
            },
            {
                "name": "memory_utilisation_percent",
                "unit": "%",
                "basis": "derived from the injected fault",
                "points": points(memory),
            },
            {
                "name": "dependency_feature_store_p95_ms",
                "unit": "ms",
                "basis": "derived from the injected fault",
                "points": points(dependency_p95),
            },
            {
                "name": "dependency_retry_rate_rps",
                "unit": "rps",
                "basis": "derived from the injected fault",
                "points": points(retry_rate),
            },
        ]

    def resource_state(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "replicas": 3,
            "desired_replicas": 3,
            "cpu_limit": "500m",
            "memory_limit": "512Mi",
            "restarts_last_hour": self.restarts,
            "source": "demo",
        }

    def recent_logs(
        self, *, window_minutes: int, level: str | None, contains: str | None, limit: int
    ) -> list[LogLine]:
        cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)
        lines: Sequence[LogLine] = [line for line in self.logs if line.at >= cutoff]
        if level is not None:
            lines = [line for line in lines if line.level == level]
        if contains is not None:
            needle = contains.lower()
            lines = [line for line in lines if needle in line.message.lower()]
        return list(lines)[-limit:]

    def ground_truth(self) -> dict[str, Any]:
        """Served only by the admin API. No agent tool points at it."""
        return {
            "scenario_id": self.scenario_id,
            "service": self.service,
            "fault": self.fault.model_dump(mode="json"),
            "injected_at": self.injected_at.isoformat() if self.injected_at else None,
            "observations": len(self.observations),
            "injections": self.injections,
        }

    def new_request_id(self) -> str:
        return uuid.uuid4().hex[:12]
