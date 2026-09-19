"""The Incident Lab: a service that really misbehaves, and the ground truth it withholds.

Everything here runs in process through ASGI, so the lab's behaviour is tested without a container.
The test that matters most is ``test_the_agent_reachable_surface_never_carries_the_answer_key``: the
read surface the platform's tools consume must not reveal which fault was injected.
"""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from opspilot.lab.faults import DeploymentChange, FaultConfig, FaultKind
from opspilot.lab.service import create_lab_app
from opspilot.retrieval.runbooks import load_runbooks
from opspilot.telemetry.http_source import READ_PATHS, HttpTelemetrySource
from opspilot.tools.action_tools import ActionLog, action_tools
from opspilot.tools.registry import ToolRegistry
from opspilot.tools.runbook_tools import runbook_tools
from opspilot.tools.telemetry_tools import telemetry_tools

TOKEN = "test-lab-token"
SERVICE = "recommendation-service"
ADMIN = {"X-Lab-Admin-Token": TOKEN}


@pytest.fixture
def app():
    return create_lab_app(admin_token=TOKEN, service_name=SERVICE)


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://lab") as http:
        yield http


async def inject(client: AsyncClient, fault: FaultConfig, deployment: DeploymentChange | None = None):
    body: dict[str, object] = {"fault": fault.model_dump(mode="json")}
    if deployment is not None:
        body["deployment"] = deployment.model_dump(mode="json")
    return await client.post("/admin/faults", json=body, headers=ADMIN)


async def test_a_healthy_service_answers_fast(client: AsyncClient) -> None:
    response = await client.post("/work")

    assert response.status_code == 200
    assert response.json()["service"] == SERVICE
    assert response.json()["latency_ms"] < 120


async def test_the_latency_fault_really_slows_the_service_down(client: AsyncClient) -> None:
    await inject(client, FaultConfig(kind=FaultKind.LATENCY, latency_ms=450))

    started = time.perf_counter()
    response = await client.post("/work")
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert response.status_code == 200
    assert elapsed_ms >= 250  # the simulated sleep is capped; the reported latency is exact
    assert response.json()["latency_ms"] >= 400


async def test_the_error_fault_really_returns_5xx(client: AsyncClient) -> None:
    await inject(client, FaultConfig(kind=FaultKind.ERRORS))

    response = await client.post("/work")

    assert response.status_code == 503


async def test_a_partial_error_rate_fails_some_requests_not_all(client: AsyncClient) -> None:
    await inject(client, FaultConfig(kind=FaultKind.HEALTHY, error_rate=0.5))

    statuses = {(await client.post("/work")).status_code for _ in range(30)}

    assert statuses == {200, 503}


async def test_the_dependency_fault_shows_up_in_logs_and_metrics(client: AsyncClient) -> None:
    await inject(
        client,
        FaultConfig(kind=FaultKind.DEPENDENCY_TIMEOUT, dependency_timeout_ms=2000, dependency_retries=3),
    )
    await client.post("/work")

    logs = (await client.get("/logs", params={"window_minutes": 5})).json()["lines"]
    metrics = (await client.get("/metrics", params={"window_minutes": 5})).json()["series"]
    by_name = {series["name"]: series for series in metrics}

    assert any("timed out" in line["message"].lower() for line in logs)
    assert any(line["level"] == "WARN" for line in logs)
    assert by_name["dependency_feature_store_p95_ms"]["points"][-1]["value"] == 2000.0
    assert by_name["dependency_retry_rate_rps"]["points"][-1]["value"] > 0.4
    assert by_name["request_latency_p95_ms"]["points"][-1]["value"] >= 400


async def test_metrics_state_their_basis(client: AsyncClient) -> None:
    series = (await client.get("/metrics")).json()["series"]

    for entry in series:
        assert entry["basis"], entry["name"]
        assert entry["points"]


async def test_the_seeded_backlog_gives_an_investigation_a_window(client: AsyncClient) -> None:
    series = (await client.get("/metrics", params={"window_minutes": 30})).json()["series"]
    latency = next(entry for entry in series if entry["name"] == "request_latency_p95_ms")
    populated = [point for point in latency["points"] if point["value"] > 0]

    assert len(populated) >= 25
    assert all(point["value"] < 200 for point in populated)


async def test_clearing_the_fault_restores_the_service(client: AsyncClient) -> None:
    await inject(client, FaultConfig(kind=FaultKind.ERRORS))
    assert (await client.post("/work")).status_code == 503

    cleared = await client.delete("/admin/faults", headers=ADMIN)

    assert cleared.status_code == 200
    assert (await client.post("/work")).status_code == 200


async def test_injection_can_carry_a_deployment(client: AsyncClient) -> None:
    await inject(
        client,
        FaultConfig(kind=FaultKind.LATENCY, latency_ms=300, note="bad rollout"),
        DeploymentChange(
            version="rec-2026.06.1",
            changes=("feature-store timeout 2000ms -> 400ms", "retries 1 -> 4"),
        ),
    )

    deployments = (await client.get("/deployments")).json()["deployments"]

    assert deployments[0]["version"] == "rec-2026.06.1"
    assert "feature-store timeout 2000ms -> 400ms" in deployments[0]["changes"]


async def test_reset_clears_everything_a_case_can_observe(app) -> None:
    """Between cases the lab must look like a fresh baseline, or an investigation explains the previous
    incident with this case's alert — which is exactly what the first live run did."""
    from opspilot.lab.faults import FaultKind

    lab = app.state.lab
    await inject(
        AsyncClient(transport=ASGITransport(app=app), base_url="http://lab"),  # type: ignore[arg-type]
        FaultConfig(kind=FaultKind.LATENCY, latency_ms=500),
        DeploymentChange(version="rec-2026.06.1", changes=("timeout cut",)),
    )
    assert lab.deployments and lab.deployments[-1].version == "rec-2026.06.1"

    lab.reset()

    assert lab.fault.kind is FaultKind.HEALTHY
    assert [change.version for change in lab.deployments] == ["rec-2026.05.9"]
    assert all("rec-2026.06.1" not in line.message for line in lab.logs)
    assert lab.observations and all(observation.synthetic for observation in lab.observations)
    assert lab.restarts == 0


async def test_the_admin_surface_requires_a_token(client: AsyncClient) -> None:
    assert (await client.get("/admin/faults")).status_code == 401
    assert (
        await client.post("/admin/faults", json={"fault": FaultConfig().model_dump(mode="json")})
    ).status_code == 401
    assert (await client.delete("/admin/faults")).status_code == 401
    assert (await client.get("/admin/ground-truth")).status_code == 401
    assert (await client.post("/admin/restart")).status_code == 401


async def test_the_agent_reachable_surface_never_carries_the_answer_key(client: AsyncClient) -> None:
    """The read surface is what the platform's tools consume: it must not name the fault or the cause."""
    fault = FaultConfig(
        kind=FaultKind.DEPENDENCY_TIMEOUT,
        dependency_timeout_ms=2400,
        dependency_retries=4,
        note="RECIPE-INTERNAL-NOTE",
        root_cause="ROOT-CAUSE-SENTINEL: the deployment lowered the dependency timeout",
        acceptable_diagnoses=("timeout too low",),
    )
    await inject(client, fault)
    await client.post("/work")

    surfaces = {
        "/metrics": (await client.get("/metrics")).text,
        "/logs": (await client.get("/logs")).text,
        "/deployments": (await client.get("/deployments")).text,
        "/state": (await client.get("/state")).text,
        "/healthz": (await client.get("/healthz")).text,
    }
    for path, body in surfaces.items():
        assert "ROOT-CAUSE-SENTINEL" not in body, path
        assert "RECIPE-INTERNAL-NOTE" not in body, path
        assert "fault injected" not in body.lower(), path
        assert "dependency_timeout" not in body, path

    truth = await client.get("/admin/ground-truth", headers=ADMIN)
    assert truth.status_code == 200
    assert truth.json()["fault"]["root_cause"].startswith("ROOT-CAUSE-SENTINEL")
    assert truth.json()["injections"]


def test_no_agent_tool_targets_the_admin_surface() -> None:
    """The control API is not reachable by any tool an agent can call."""
    runbooks_dir = Path(__file__).resolve().parents[2] / "runbooks"
    registry = ToolRegistry(
        telemetry_tools(HttpTelemetrySource("http://lab"))
        + runbook_tools(load_runbooks(runbooks_dir))
        + action_tools(ActionLog())
    )

    snapshot = registry.governance_snapshot()
    rendered = json.dumps(snapshot).lower()

    assert snapshot
    assert "/admin" not in rendered
    assert "ground-truth" not in rendered
    assert "ground_truth" not in rendered
    # The lab's read surface, which the adapter is built on, contains no admin path.
    assert READ_PATHS == ("/metrics", "/logs", "/deployments", "/state")
    assert all(not path.startswith("/admin") for path in READ_PATHS)


def test_the_read_surface_has_no_write_method() -> None:
    """Reading telemetry must not be able to change the service: every read path is a GET."""
    from opspilot.telemetry import http_source

    source = inspect.getsource(http_source)
    for path in READ_PATHS:
        assert f'_get("{path}"' in source, path
    assert "client.post" not in source
    assert "client.delete" not in source
