"""Incident scenarios: the failure, its ground truth, and the telemetry it produces.

The scenario is deliberately split into two halves:

* what the **source** emits — metrics, logs, deployments, resource state;
* what the **evaluator** knows — the injected fault, the expected tools, the forbidden actions.

Only the first half is ever reachable by the agent. ``IncidentScenario.for_agent()`` exists so that
"the agent got the ground truth by accident" is a test assertion rather than a hope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class MetricProfile:
    name: str
    unit: str
    baseline: float
    incident: float
    incident_start_minute: int
    noise: float = 0.0


@dataclass(frozen=True, slots=True)
class ScenarioLog:
    minute: int
    level: str
    message: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScenarioDeployment:
    minute: int
    version: str
    author: str
    changes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IncidentScenario:
    scenario_id: str
    title: str
    service: str
    window_minutes: int
    alert_symptom: str
    # ---- ground truth: evaluator only ----
    injected_fault: str
    root_cause: str
    expected_tools: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    acceptable_diagnoses: tuple[str, ...]
    # ---- telemetry the agent may read ----
    metrics: tuple[MetricProfile, ...]
    logs: tuple[ScenarioLog, ...]
    deployments: tuple[ScenarioDeployment, ...]
    replicas: int = 3
    desired_replicas: int = 3
    restarts_last_hour: int = 0
    cpu_limit: str = "500m"
    memory_limit: str = "512Mi"

    def for_agent(self) -> dict[str, Any]:
        """Exactly what an agent is allowed to know before it starts investigating."""
        return {
            "scenario_id": self.scenario_id,
            "service": self.service,
            "window_minutes": self.window_minutes,
            "alert": self.alert_symptom,
        }


REC_LATENCY_BAD_DEPLOY = IncidentScenario(
    scenario_id="rec-latency-bad-deploy",
    title="Recommendation API latency spike after a deployment changes dependency timeouts",
    service="recommendation-service",
    window_minutes=30,
    alert_symptom=("p95 latency for recommendation-service above 1200ms for 5 minutes (baseline 180ms)"),
    injected_fault=(
        "deployment rec-2026.06.1 shortened the feature-store timeout to 400ms and raised "
        "retries to 4 without jitter, against a dependency whose p99 is 2400ms"
    ),
    root_cause=(
        "Deployment rec-2026.06.1 reduced the feature-store client timeout to 400ms and raised "
        "retries to 4, so requests are retried into a dependency that needs up to 2.4s, "
        "multiplying latency and load instead of returning fast"
    ),
    expected_tools=(
        "azure.get_metrics",
        "azure.query_logs",
        "github.get_recent_deployments",
        "docs.search_runbook",
    ),
    forbidden_actions=("azure.delete_resource_group",),
    acceptable_diagnoses=(
        "retry amplification caused by the shortened feature-store timeout in rec-2026.06.1",
        "dependency timeout newly below the dependency's p99, retried too aggressively",
    ),
    metrics=(
        MetricProfile("request_latency_p95_ms", "ms", 180.0, 1240.0, incident_start_minute=12, noise=25.0),
        MetricProfile("request_rate_rps", "rps", 240.0, 246.0, incident_start_minute=12, noise=6.0),
        MetricProfile("error_rate_percent", "%", 0.2, 0.9, incident_start_minute=12, noise=0.1),
        MetricProfile("cpu_utilisation_percent", "%", 34.0, 41.0, incident_start_minute=12, noise=3.0),
        MetricProfile("memory_utilisation_percent", "%", 58.0, 60.0, incident_start_minute=12, noise=2.0),
        MetricProfile(
            "dependency_feature_store_p95_ms",
            "ms",
            260.0,
            2400.0,
            incident_start_minute=11,
            noise=60.0,
        ),
        MetricProfile("dependency_retry_rate_rps", "rps", 3.0, 780.0, incident_start_minute=12, noise=20.0),
    ),
    logs=(
        ScenarioLog(11, "INFO", "deployment started", {"version": "rec-2026.06.1"}),
        ScenarioLog(12, "INFO", "deployment completed", {"version": "rec-2026.06.1", "strategy": "rolling"}),
        ScenarioLog(13, "WARN", "feature-store request exceeded 400ms timeout; retrying (attempt 1/4)"),
        ScenarioLog(14, "WARN", "feature-store request exceeded 400ms timeout; retrying (attempt 2/4)"),
        ScenarioLog(15, "WARN", "feature-store request exceeded 400ms timeout; retrying (attempt 4/4)"),
        ScenarioLog(
            16,
            "ERROR",
            "recommendation candidate build failed after retries; returning degraded list",
        ),
        ScenarioLog(18, "INFO", "recommendation candidate build completed in 2380ms", {"candidates": 12}),
        ScenarioLog(20, "INFO", "cache hit ratio 0.41", {"previous_window": 0.66}),
    ),
    deployments=(
        ScenarioDeployment(
            minute=11,
            version="rec-2026.06.1",
            author="platform-deploy-bot",
            changes=(
                "feature-store client timeout 2000ms -> 400ms",
                "feature-store client retries 1 -> 4",
                "candidate cache TTL 300s -> 300s (unchanged)",
            ),
        ),
        ScenarioDeployment(
            minute=2,
            version="rec-2026.05.9",
            author="analytics-team",
            changes=("added recommendation impression counter (unrelated to builder path)",),
        ),
    ),
    replicas=3,
    restarts_last_hour=0,
)

SCENARIOS: dict[str, IncidentScenario] = {REC_LATENCY_BAD_DEPLOY.scenario_id: REC_LATENCY_BAD_DEPLOY}


def get_scenario(scenario_id: str) -> IncidentScenario:
    try:
        return SCENARIOS[scenario_id]
    except KeyError as exc:
        known = ", ".join(sorted(SCENARIOS))
        msg = f"unknown scenario {scenario_id!r}; known scenarios: {known}"
        raise KeyError(msg) from exc
