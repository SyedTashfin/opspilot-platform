"""The HTTP telemetry source: translation, labelling, and refusal to hide a failure.

The same tools are pointed at a running lab service here, which is the claim M4 made when the tools were
built as factories over an injected source: swapping telemetry backends does not touch a step.
"""

from __future__ import annotations

import httpx
import pytest

from opspilot.lab.faults import FaultConfig, FaultKind
from opspilot.lab.service import create_lab_app
from opspilot.telemetry.http_source import READ_PATHS, HttpTelemetrySource
from opspilot.telemetry.source import TelemetryUnavailableError
from opspilot.tools.audit import InMemoryAuditRecorder
from opspilot.tools.executor import ToolExecutor
from opspilot.tools.registry import ToolRegistry
from opspilot.tools.telemetry_tools import (
    DeploymentsResult,
    LogQueryResult,
    MetricsResult,
    ResourceStateResult,
    telemetry_tools,
)
from opspilot.tools.types import ToolContext

TOKEN = "test-lab-token"
SERVICE = "recommendation-service"
ADMIN = {"X-Lab-Admin-Token": TOKEN}


@pytest.fixture
def app():
    return create_lab_app(admin_token=TOKEN, service_name=SERVICE)


@pytest.fixture
def source(app) -> HttpTelemetrySource:
    return HttpTelemetrySource(
        "http://lab",
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
    )


async def inject(app, fault: FaultConfig) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://lab",
    ) as client:
        await client.post("/admin/faults", json={"fault": fault.model_dump(mode="json")}, headers=ADMIN)


async def test_metrics_are_translated_into_platform_types(source: HttpTelemetrySource) -> None:
    snapshot = await source.metrics(SERVICE, 30)

    assert snapshot.service == SERVICE
    assert snapshot.window_minutes == 30
    assert snapshot.source == "demo"
    names = {series.name for series in snapshot.series}
    assert "request_latency_p95_ms" in names
    latency = next(series for series in snapshot.series if series.name == "request_latency_p95_ms")
    assert latency.unit == "ms"
    assert latency.points
    assert latency.points[0].at.tzinfo is not None


async def test_logs_deployments_and_state_are_translated(source: HttpTelemetrySource) -> None:
    logs = await source.logs(SERVICE, 30)
    deployments = await source.deployments(SERVICE)
    state = await source.resource_state(SERVICE)

    assert logs and logs[0].service == SERVICE
    assert deployments and deployments[0].version
    assert deployments[0].deployed_at.tzinfo is not None
    assert state.replicas == 3
    assert state.source == "demo"


async def test_injected_latency_is_visible_through_the_adapter(app, source) -> None:
    await inject(app, FaultConfig(kind=FaultKind.LATENCY, latency_ms=500))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://lab",
    ) as client:
        await client.post("/work")

    snapshot = await source.metrics(SERVICE, 5)
    latency = next(series for series in snapshot.series if series.name == "request_latency_p95_ms")

    assert latency.points[-1].value >= 400


async def test_log_filters_pass_through(app, source) -> None:
    await inject(app, FaultConfig(kind=FaultKind.DEPENDENCY_TIMEOUT, dependency_timeout_ms=1500))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://lab",
    ) as client:
        await client.post("/work")

    warnings = await source.logs(SERVICE, 5, level="WARN", contains="timed out", limit=5)
    infos = await source.logs(SERVICE, 5, level="INFO", limit=5)

    assert warnings and all(line.level == "WARN" for line in warnings)
    assert all("timed out" in line.message.lower() for line in warnings)
    assert all(line.level == "INFO" for line in infos)


async def test_telemetry_for_another_service_is_refused(source: HttpTelemetrySource) -> None:
    with pytest.raises(TelemetryUnavailableError, match="refusing to mix telemetry"):
        await source.metrics("some-other-service", 30)


async def test_an_unreachable_service_raises_instead_of_returning_nothing() -> None:
    def refusing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    source = HttpTelemetrySource("http://nothing", transport=httpx.MockTransport(refusing))

    with pytest.raises(TelemetryUnavailableError, match="failed"):
        await source.metrics(SERVICE, 30)
    with pytest.raises(TelemetryUnavailableError):
        await source.logs(SERVICE, 30)
    with pytest.raises(TelemetryUnavailableError):
        await source.deployments(SERVICE)
    with pytest.raises(TelemetryUnavailableError):
        await source.resource_state(SERVICE)


async def test_a_non_json_response_raises_instead_of_guessing() -> None:
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>", request=request)

    source = HttpTelemetrySource("http://proxy", transport=httpx.MockTransport(html))

    with pytest.raises(TelemetryUnavailableError, match="not JSON"):
        await source.metrics(SERVICE, 30)


async def test_an_http_error_raises() -> None:
    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom", request=request)

    source = HttpTelemetrySource("http://proxy", transport=httpx.MockTransport(server_error))

    with pytest.raises(TelemetryUnavailableError, match="failed"):
        await source.metrics(SERVICE, 30)


async def test_the_platform_tools_work_unchanged_over_http(app, source) -> None:
    """The M4 claim: same tools, different telemetry backend."""
    await inject(app, FaultConfig(kind=FaultKind.ERRORS, error_rate=1.0))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://lab",
    ) as client:
        await client.post("/work")

    executor = ToolExecutor(registry=ToolRegistry(telemetry_tools(source)), audit=InMemoryAuditRecorder())
    context = ToolContext(agent="opspilot")

    metrics = await executor.execute("azure.get_metrics", {"service": SERVICE}, context)
    logs = await executor.execute("azure.query_logs", {"service": SERVICE, "level": "ERROR"}, context)
    deployments = await executor.execute("github.get_recent_deployments", {"service": SERVICE}, context)
    state = await executor.execute("azure.get_resource_state", {"service": SERVICE}, context)

    assert metrics.status.value == "ok"
    assert isinstance(metrics.output, MetricsResult)
    assert metrics.output.source == "demo"
    assert isinstance(logs.output, LogQueryResult)
    assert logs.output.lines and logs.output.lines[0].level == "ERROR"
    assert isinstance(deployments.output, DeploymentsResult)
    assert deployments.output.deployments
    assert isinstance(state.output, ResourceStateResult)
    assert state.output.source == "demo"


def test_the_adapter_only_reads() -> None:
    from opspilot.telemetry import http_source

    body = __import__("inspect").getsource(http_source)

    assert READ_PATHS == ("/metrics", "/logs", "/deployments", "/state")
    assert all(not path.startswith("/admin") for path in READ_PATHS)
    assert "client.post" not in body
    assert "client.delete" not in body
    assert "put(" not in body
