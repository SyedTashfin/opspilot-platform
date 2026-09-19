"""Spans and platform metrics against real PostgreSQL.

The aggregate SQL is where a subtle bug hides — a wrong ``case`` in "spend today", a missing status in
the success rate — so it is checked against rows this test inserted and can therefore count by hand.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import opspilot.db.models as models
from opspilot.agent.store import PostgresRunStore
from opspilot.domain.enums import AgentStatus, ApprovalDecision, RunStatus, StepStatus
from opspilot.observability.metrics import summarise, summarise_trace
from opspilot.observability.repository import PostgresMetricsRepository, PostgresTraceRepository
from opspilot.observability.spans import SpanRecord

pytestmark = pytest.mark.integration

START = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def make_span(name: str, *, offset_ms: int, duration_ms: int, parent: str | None = None, **attrs):
    started = START + timedelta(milliseconds=offset_ms)
    return SpanRecord(
        trace_id="d" * 32,
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=parent,
        name=name,
        kind="internal",
        started_at=started,
        ended_at=started + timedelta(milliseconds=duration_ms),
        duration_ms=duration_ms,
        status="ok",
        attributes=dict(attrs),
    )


async def seed_agent(session: AsyncSession, name: str) -> models.Agent:
    agent = models.Agent(
        name=name,
        description="spans test",
        status=AgentStatus.ACTIVE,
        allowed_tools=["azure.get_metrics"],
    )
    session.add(agent)
    await session.flush()
    return agent


async def test_spans_round_trip_through_postgres(live_session: AsyncSession) -> None:
    agent = await seed_agent(live_session, f"opspilot-{uuid.uuid4().hex[:8]}")
    store = PostgresRunStore(live_session)
    run_id = await store.create_run(agent=agent.name, request={}, model="fake/fake-1")
    await store.attach_trace(run_id=run_id, trace_id="d" * 32)

    run_span = make_span("agent.run", offset_ms=0, duration_ms=900, **{"run.status": "succeeded"})
    step_span = make_span(
        "agent.step.diagnose",
        offset_ms=10,
        duration_ms=700,
        parent=run_span.span_id,
        agent_step="diagnose",
    )
    model_span = make_span(
        "gateway.complete",
        offset_ms=20,
        duration_ms=650,
        parent=step_span.span_id,
        llm_model="fake/fake-1",
        llm_input_tokens=120,
        llm_cost_known=True,
    )
    await store.record_spans(run_id=run_id, spans=[model_span, run_span, step_span])
    await live_session.commit()

    data = await PostgresTraceRepository(live_session).trace(run_id)
    view = summarise_trace(data)

    assert data.trace_id == "d" * 32
    assert view.span_count == 3
    assert view.slowest_span == "agent.run"
    assert [row.name for row in view.spans] == [
        "agent.run",
        "agent.step.diagnose",
        "gateway.complete",
    ]
    assert view.spans[2].attributes["llm_input_tokens"] == 120
    assert view.spans[1].parent_span_id == run_span.span_id


async def test_metrics_aggregate_what_the_tables_hold(live_engine) -> None:
    factory = async_sessionmaker(live_engine, expire_on_commit=False)
    async with factory() as session:
        agent = await seed_agent(session, f"opspilot-{uuid.uuid4().hex[:8]}")

        succeeded = models.Run(
            agent_id=agent.id, status=RunStatus.SUCCEEDED, duration_ms=1000, trigger="test"
        )
        failed = models.Run(agent_id=agent.id, status=RunStatus.FAILED, duration_ms=3000, trigger="test")
        running = models.Run(agent_id=agent.id, status=RunStatus.RUNNING, trigger="test")
        session.add_all([succeeded, failed, running])
        await session.flush()

        yesterday = datetime.now(UTC) - timedelta(days=1)
        session.add_all(
            [
                models.ModelCall(
                    run_id=succeeded.id,
                    step="diagnose",
                    provider="fake",
                    model="fake/fake-1",
                    input_tokens=100,
                    output_tokens=50,
                    cost_eur=0.001,
                    created_at=datetime.now(UTC),
                ),
                models.ModelCall(
                    run_id=failed.id,
                    step="classify",
                    provider="fake",
                    model="fake/fake-1",
                    input_tokens=200,
                    output_tokens=10,
                    cost_eur=0.002,
                    created_at=yesterday,
                ),
            ]
        )
        session.add_all(
            [
                models.RunStep(
                    run_id=succeeded.id,
                    step_index=0,
                    name="classify",
                    status=StepStatus.SUCCEEDED,
                    duration_ms=100,
                ),
                models.RunStep(
                    run_id=succeeded.id,
                    step_index=1,
                    name="diagnose",
                    status=StepStatus.SUCCEEDED,
                    duration_ms=300,
                ),
            ]
        )
        session.add(
            models.Approval(
                run_id=succeeded.id,
                tool_name="azure.restart_service",
                token="single-use-approval-token",
                arguments_hash="deadbeef",
                decision=ApprovalDecision.PENDING,
                requested_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(seconds=900),
            )
        )
        session.add(
            models.Span(
                run_id=succeeded.id,
                trace_id="e" * 32,
                span_id=uuid.uuid4().hex[:16],
                parent_span_id=None,
                name="agent.run",
                kind="internal",
                status="ok",
                started_at=START,
                ended_at=START,
                duration_ms=1000,
                attributes={"source": "demo", "run.status": "succeeded"},
            )
        )
        await session.commit()

        facts = await PostgresMetricsRepository(session).facts(environment="test")
        overview = summarise(facts)

    assert facts.runs_by_status == {"succeeded": 1, "failed": 1, "running": 1}
    assert overview.runs_total.value == 3.0
    assert overview.success_rate.value == 0.5
    assert facts.spend_today_eur == pytest.approx(0.001)
    assert facts.spend_total_eur == pytest.approx(0.003)
    assert facts.model_calls_total == 2
    assert facts.input_tokens == 300
    assert facts.output_tokens == 60
    assert sorted(facts.step_latencies_ms) == [100, 300]
    assert overview.mean_step_latency_ms.value == 200.0
    assert sorted(facts.run_durations_ms) == [1000, 3000]
    assert overview.p95_run_duration_ms.value == 3000.0
    assert facts.spans_total == 1
    assert facts.approvals_pending == 1
    assert "demo" in overview.data_sources
    assert overview.data_sources[-1] == "postgres"


async def test_deleting_a_run_deletes_its_spans(live_session: AsyncSession) -> None:
    """Spans belong to a run; the schema says so, and the cascade is what enforces it."""
    agent = await seed_agent(live_session, f"opspilot-{uuid.uuid4().hex[:8]}")
    store = PostgresRunStore(live_session)
    run_id = await store.create_run(agent=agent.name, request={}, model=None)
    await store.record_spans(run_id=run_id, spans=[make_span("agent.run", offset_ms=0, duration_ms=10)])
    await live_session.commit()

    run = await live_session.get(models.Run, run_id)
    assert run is not None
    await live_session.delete(run)
    await live_session.commit()

    remaining = await PostgresTraceRepository(live_session).trace(run_id)
    assert remaining.spans == []
