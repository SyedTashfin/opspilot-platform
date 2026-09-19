from __future__ import annotations

import json

import pytest

from opspilot.telemetry.scenarios import SCENARIOS, get_scenario
from opspilot.telemetry.synthetic import SyntheticTelemetrySource

SCENARIO = get_scenario("rec-latency-bad-deploy")
SERVICE = SCENARIO.service


def test_scenarios_are_registered_and_self_describing() -> None:
    assert SCENARIOS["rec-latency-bad-deploy"] is SCENARIO
    assert SCENARIO.alert_symptom
    assert SCENARIO.injected_fault and SCENARIO.root_cause
    assert SCENARIO.acceptable_diagnoses
    assert SCENARIO.forbidden_actions


def test_agent_view_excludes_ground_truth() -> None:
    """The agent is given the alert. The fault and the root cause stay in the harness."""
    view = SCENARIO.for_agent()
    assert set(view) == {"scenario_id", "service", "window_minutes", "alert"}

    serialized = json.dumps(view)
    assert SCENARIO.injected_fault not in serialized
    assert SCENARIO.root_cause not in serialized
    assert all(term not in serialized for term in SCENARIO.acceptable_diagnoses)


async def test_tool_outputs_do_not_leak_ground_truth() -> None:
    """Metrics, logs and deployments must look like telemetry, not like an answer key."""
    source = SyntheticTelemetrySource(scenario=SCENARIO)
    snapshot = await source.metrics(SERVICE, 30)
    lines = await source.logs(SERVICE, 30)
    deployments = await source.deployments(SERVICE)
    payload = {
        "metrics": [series.name for series in snapshot.series],
        "logs": [line.message for line in lines],
        "deployments": [change for deployment in deployments for change in deployment.changes],
        "resource": str(await source.resource_state(SERVICE)),
    }
    serialized = json.dumps(payload)
    assert SCENARIO.injected_fault not in serialized
    assert SCENARIO.root_cause not in serialized


async def test_metric_generation_is_deterministic() -> None:
    first = await SyntheticTelemetrySource(scenario=SCENARIO).metrics(SERVICE, 30)
    second = await SyntheticTelemetrySource(scenario=SCENARIO).metrics(SERVICE, 30)
    assert first == second
    assert first.source == "demo"
    assert first.window_minutes == 30
    assert {series.name for series in first.series} == {
        profile.name for profile in SCENARIO.metrics
    }


async def test_the_injected_fault_is_visible_in_the_metrics_it_should_move() -> None:
    snapshot = await SyntheticTelemetrySource(scenario=SCENARIO).metrics(SERVICE, 30)
    by_name = {series.name: series for series in snapshot.series}
    profile = next(row for row in SCENARIO.metrics if row.name == "request_latency_p95_ms")
    points = sorted(by_name["request_latency_p95_ms"].points, key=lambda point: point.at)

    before = [point.value for point in points[: profile.incident_start_minute]]
    after = [point.value for point in points[profile.incident_start_minute :]]
    assert before and after
    assert max(before) < min(after)


async def test_unaffected_metrics_stay_flat() -> None:
    snapshot = await SyntheticTelemetrySource(scenario=SCENARIO).metrics(SERVICE, 30)
    by_name = {series.name: series for series in snapshot.series}
    profile = next(row for row in SCENARIO.metrics if row.name == "cpu_utilisation_percent")
    values = [point.value for point in by_name["cpu_utilisation_percent"].points]
    assert max(values) < profile.baseline * 2


async def test_the_deployment_precedes_the_symptom() -> None:
    deployments = await SyntheticTelemetrySource(scenario=SCENARIO).deployments(SERVICE)
    newest = max(deployments, key=lambda deployment: deployment.deployed_at)
    profile = next(row for row in SCENARIO.metrics if row.name == "request_latency_p95_ms")
    assert newest.version.startswith("rec-")
    assert newest.changes
    assert profile.incident_start_minute > 0


async def test_log_filtering_and_limits() -> None:
    source = SyntheticTelemetrySource(scenario=SCENARIO)
    everything = await source.logs(SERVICE, 30)
    assert len(everything) == len(SCENARIO.logs)

    warnings = await source.logs(SERVICE, 30, level="WARN")
    assert warnings and all(line.level == "WARN" for line in warnings)

    timeouts = await source.logs(SERVICE, 30, contains="timeout")
    assert timeouts and all("timeout" in line.message.lower() for line in timeouts)

    assert len(await source.logs(SERVICE, 30, limit=2)) == 2
    assert len(await source.logs(SERVICE, 1)) <= len(everything)


async def test_an_unknown_service_is_refused() -> None:
    source = SyntheticTelemetrySource(scenario=SCENARIO)
    with pytest.raises(KeyError):
        await source.metrics("not-our-service", 30)


async def test_deployments_are_returned_newest_first_and_respect_the_limit() -> None:
    source = SyntheticTelemetrySource(scenario=SCENARIO)
    assert len(await source.deployments(SERVICE, limit=1)) == 1
    all_deployments = await source.deployments(SERVICE, limit=10)
    assert [row.deployed_at for row in all_deployments] == sorted(
        (row.deployed_at for row in all_deployments), reverse=True
    )
