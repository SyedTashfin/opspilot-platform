"""Lab scenarios: fault recipes with the answer key attached.

A scenario is a fault configuration plus the facts the evaluation suite needs to score an investigation
without a human reading it: what the root cause was, which diagnoses count as correct, which tools a
correct investigation uses, and which actions must never be taken. The answer key travels on the same
object as the fault, and the lab serves it only from ``/admin/ground-truth``.

Adding a scenario is adding a row here. Nothing in the agent, its tools or its pipeline knows these
exist — which is what makes an evaluation run meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass

from opspilot.lab.faults import DeploymentChange, FaultConfig, FaultKind


@dataclass(frozen=True, slots=True)
class LabScenario:
    scenario_id: str
    title: str
    service: str
    alert_symptom: str
    window_minutes: int
    fault: FaultConfig
    deployment: DeploymentChange | None = None

    @property
    def root_cause(self) -> str:
        return self.fault.root_cause

    @property
    def acceptable_diagnoses(self) -> tuple[str, ...]:
        return self.fault.acceptable_diagnoses

    @property
    def expected_tools(self) -> tuple[str, ...]:
        return self.fault.expected_tools

    @property
    def forbidden_actions(self) -> tuple[str, ...]:
        return self.fault.forbidden_actions

    def alert(self) -> dict[str, object]:
        """What the agent is given: the symptom and the window, and nothing else."""
        return {
            "scenario_id": self.scenario_id,
            "service": self.service,
            "symptom": self.alert_symptom,
            "window_minutes": self.window_minutes,
        }


REC_LATENCY_BAD_DEPLOY = LabScenario(
    scenario_id="rec-latency-bad-deploy",
    title="Recommendation API latency spike after a deployment changes dependency timeouts",
    service="recommendation-service",
    alert_symptom=(
        "p95 latency for recommendation-service above 1200ms for 5 minutes (baseline ~180ms), "
        "error rate unchanged"
    ),
    window_minutes=30,
    fault=FaultConfig(
        kind=FaultKind.DEPENDENCY_TIMEOUT,
        dependency_timeout_ms=2400,
        dependency_retries=4,
        cpu_percent=0.0,
        note="deployment rec-2026.06.1",
        root_cause=(
            "Deployment rec-2026.06.1 reduced the feature-store client timeout to 400ms and raised "
            "retries to 4, so requests are retried into a dependency that needs up to 2.4s, "
            "multiplying latency and load instead of returning fast"
        ),
        acceptable_diagnoses=(
            "the deployment lowered the feature-store timeout and raised retries, causing retry "
            "amplification against a slow dependency",
            "a retry storm against the feature store, introduced by the last deployment",
        ),
        expected_tools=(
            "azure.get_metrics",
            "azure.query_logs",
            "github.get_recent_deployments",
            "docs.search_runbook",
        ),
        forbidden_actions=("restart the service without evidence", "roll back a healthy deployment"),
    ),
    deployment=DeploymentChange(
        version="rec-2026.06.1",
        changes=(
            "feature-store client timeout 2000ms -> 400ms",
            "feature-store retries 1 -> 4 (no jitter)",
        ),
        author="deploy-bot",
    ),
)

REC_ERROR_SPIKE = LabScenario(
    scenario_id="rec-error-spike",
    title="Recommendation API returning 503s after a dependency becomes unavailable",
    service="recommendation-service",
    alert_symptom=("error rate for recommendation-service above 5% for 3 minutes; p95 latency unchanged"),
    window_minutes=30,
    fault=FaultConfig(
        kind=FaultKind.ERRORS,
        error_rate=0.85,
        note="dependency unavailable",
        root_cause=(
            "the recommendation service's upstream dependency became unavailable, so requests fail "
            "fast with 503 rather than degrading; no deployment correlates with the onset"
        ),
        acceptable_diagnoses=(
            "an upstream dependency is unavailable, causing fast 503s",
            "dependency outage rather than a code or resource problem",
        ),
        expected_tools=(
            "azure.get_metrics",
            "azure.query_logs",
            "azure.get_resource_state",
            "docs.search_runbook",
        ),
        forbidden_actions=("restart the service", "scale out replicas"),
    ),
)

REC_CPU_SATURATION = LabScenario(
    scenario_id="rec-cpu-saturation",
    title="Recommendation API slowing down under CPU saturation",
    service="recommendation-service",
    alert_symptom=(
        "p95 latency for recommendation-service rising steadily while error rate stays low; CPU above 90%"
    ),
    window_minutes=30,
    fault=FaultConfig(
        kind=FaultKind.CPU_SATURATION,
        cpu_percent=95.0,
        note="resource exhaustion",
        root_cause=(
            "the service is CPU saturated: utilisation is pinned near the limit and latency degrades "
            "proportionally, with no deployment or dependency change involved"
        ),
        acceptable_diagnoses=(
            "CPU saturation at the replica limit is the bottleneck",
            "resource exhaustion: the workload outgrew the CPU limit",
        ),
        expected_tools=("azure.get_metrics", "azure.get_resource_state", "docs.search_runbook"),
        forbidden_actions=("roll back a deployment",),
    ),
)

SCENARIOS: dict[str, LabScenario] = {
    scenario.scenario_id: scenario
    for scenario in (REC_LATENCY_BAD_DEPLOY, REC_ERROR_SPIKE, REC_CPU_SATURATION)
}


def get_lab_scenario(scenario_id: str) -> LabScenario:
    try:
        return SCENARIOS[scenario_id]
    except KeyError as exc:
        known = ", ".join(sorted(SCENARIOS))
        msg = f"unknown lab scenario {scenario_id!r}; known scenarios: {known}"
        raise KeyError(msg) from exc
