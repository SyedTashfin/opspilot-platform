"""The observability endpoints, tested through the HTTP contract with stubbed data sources.

The endpoint is tested without a database and the query layer is tested against a real one
(``tests/integration``), so a failure here means the contract broke, not the SQL.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient

from opspilot.api.main import create_app
from opspilot.api.routes import platform
from opspilot.observability.metrics import PlatformFacts, TraceData
from opspilot.observability.spans import SpanRecord

START = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


class StubMetrics:
    def __init__(self, facts: PlatformFacts) -> None:
        self.facts_data = facts

    async def facts(self, *, environment: str) -> PlatformFacts:
        return replace(self.facts_data, environment=environment)


class StubTraces:
    def __init__(self, data: TraceData) -> None:
        self.data = data

    async def trace(self, run_id: uuid.UUID) -> TraceData:
        return replace(self.data, run_id=run_id)


def span(name: str, *, offset_ms: int, duration_ms: int, parent: str | None = None, **attrs):
    started = START + timedelta(milliseconds=offset_ms)
    return SpanRecord(
        trace_id="c" * 32,
        span_id=f"{name[:14]:<14}"[:16],
        parent_span_id=parent,
        name=name,
        kind="internal",
        started_at=started,
        ended_at=started + timedelta(milliseconds=duration_ms),
        duration_ms=duration_ms,
        status="ok",
        attributes=dict(attrs),
    )


async def call(path: str, *, facts: PlatformFacts | None = None, trace: TraceData | None = None):
    app = create_app()
    if facts is not None:
        app.dependency_overrides[platform.metrics_source] = lambda: StubMetrics(facts)
    if trace is not None:
        app.dependency_overrides[platform.trace_source] = lambda: StubTraces(trace)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


async def test_overview_returns_measured_numbers_with_a_basis_each() -> None:
    facts = PlatformFacts(
        environment="ci",
        runs_by_status={"succeeded": 2, "waiting_approval": 1},
        spend_today_eur=0.001234,
        spend_total_eur=0.009,
        model_calls_total=5,
        input_tokens=900,
        output_tokens=100,
        step_latencies_ms=[40, 60],
        run_durations_ms=[100, 200, 300],
        spans_total=18,
        approvals_pending=1,
        scenario_labels=["demo"],
    )

    response = await call("/api/v1/platform/overview", facts=facts)

    assert response.status_code == 200
    body = response.json()
    assert body["environment"] == "ci"
    assert body["runs_total"]["value"] == 3.0
    assert body["runs_total"]["source"] == "measured"
    assert "count(runs)" in body["runs_total"]["basis"]
    assert body["spend_today_eur"]["unit"] == "EUR"
    assert body["tokens_total"]["value"] == 1000.0
    assert body["mean_step_latency_ms"]["value"] == 50.0
    assert body["data_sources"] == ["demo", "postgres"]
    assert body["runs_by_status"] == {"succeeded": 2, "waiting_approval": 1}


async def test_trace_endpoint_returns_ordered_spans_with_their_parents() -> None:
    run_id = uuid.uuid4()
    run_span = span("agent.run", offset_ms=0, duration_ms=900)
    step_span = span("agent.step.diagnose", offset_ms=10, duration_ms=700, parent=run_span.span_id)
    model_span = span(
        "gateway.complete",
        offset_ms=20,
        duration_ms=650,
        parent=step_span.span_id,
        llm_model="fake/fake-1",
        llm_input_tokens=120,
    )

    response = await call(
        f"/api/v1/runs/{run_id}/trace",
        trace=TraceData(run_id=run_id, trace_id="c" * 32, spans=[model_span, run_span, step_span]),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(run_id)
    assert body["trace_id"] == "c" * 32
    assert body["span_count"] == 3
    assert body["error_count"] == 0
    assert [row["name"] for row in body["spans"]] == [
        "agent.run",
        "agent.step.diagnose",
        "gateway.complete",
    ]
    assert body["spans"][2]["attributes"]["llm_input_tokens"] == 120
    assert body["slowest_span"] == "agent.run"


async def test_trace_endpoint_reports_an_unknown_run_rather_than_an_empty_trace() -> None:
    run_id = uuid.uuid4()

    response = await call(f"/api/v1/runs/{run_id}/trace", trace=TraceData(run_id=run_id))

    assert response.status_code == 404
    assert str(run_id) in response.json()["detail"]


async def test_openapi_lists_the_platform_routes() -> None:
    app = create_app()
    schema = app.openapi()

    assert {"/api/v1/platform/overview", "/api/v1/runs/{run_id}/trace"} <= set(schema["paths"])
