"""The persisted run store, exercised against real PostgreSQL.

This is the store the API layer will use, so it is tested where it actually runs: the schema, the
foreign keys, and the summary aggregate that ``finish_run`` maintains over ``model_calls``.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import opspilot.db.models as models
from opspilot.agent.store import PostgresRunStore, UnknownAgentError
from opspilot.domain.enums import AgentStatus, RunStatus, StepStatus

pytestmark = pytest.mark.integration


async def _seed_agent(session: AsyncSession, name: str) -> uuid.UUID:
    agent = models.Agent(
        name=name,
        description="integration test agent",
        status=AgentStatus.ACTIVE,
        allowed_tools=["azure.get_metrics"],
    )
    session.add(agent)
    await session.flush()
    await session.commit()
    return agent.id


async def test_unknown_agent_is_refused(live_session: AsyncSession) -> None:
    store = PostgresRunStore(live_session)

    with pytest.raises(UnknownAgentError):
        await store.create_run(agent="not-registered", request={"service": "api"}, model=None)


async def test_run_and_step_lifecycle_is_persisted(live_session: AsyncSession) -> None:
    agent_name = f"opspilot-{uuid.uuid4().hex[:8]}"
    await _seed_agent(live_session, agent_name)
    store = PostgresRunStore(live_session)

    run_id = await store.create_run(
        agent=agent_name, request={"service": "recommendation-service"}, model="fake/fake-1"
    )
    step_id = await store.create_step(run_id=run_id, index=0, name="classify")
    await store.finish_step(
        step_id=step_id,
        status=StepStatus.SUCCEEDED,
        summary="classified as latency",
        detail={"incident_class": "latency"},
        duration_ms=42,
    )
    await live_session.commit()

    records = await store.steps(run_id)
    assert [record.name for record in records] == ["classify"]
    assert records[0].status is StepStatus.SUCCEEDED
    assert records[0].summary == "classified as latency"
    assert records[0].detail == {"incident_class": "latency"}
    assert records[0].duration_ms == 42

    run = await live_session.get(models.Run, run_id)
    assert run is not None and run.status is RunStatus.RUNNING

    await store.finish_run(run_id=run_id, status=RunStatus.SUCCEEDED, duration_ms=1234)
    await live_session.commit()

    finished = await live_session.get(models.Run, run_id)
    assert finished is not None
    assert finished.status is RunStatus.SUCCEEDED
    assert finished.duration_ms == 1234
    assert finished.completed_at is not None


async def test_finish_run_summarises_model_calls(live_engine) -> None:
    """Run-level tokens and cost are a projection of model_calls, not a second ledger."""
    factory = async_sessionmaker(live_engine, expire_on_commit=False)
    async with factory() as session:
        agent_name = f"opspilot-{uuid.uuid4().hex[:8]}"
        await _seed_agent(session, agent_name)
        store = PostgresRunStore(session)
        run_id = await store.create_run(agent=agent_name, request={}, model="fake/fake-1")

        session.add_all(
            [
                models.ModelCall(
                    run_id=run_id,
                    step="classify",
                    provider="fake",
                    model="fake/fake-1",
                    input_tokens=100,
                    output_tokens=20,
                    cost_eur=0.00025,
                ),
                models.ModelCall(
                    run_id=run_id,
                    step="diagnose",
                    provider="fake",
                    model="fake/fake-1",
                    input_tokens=400,
                    output_tokens=80,
                    cost_eur=0.00100,
                ),
            ]
        )
        await session.commit()

        await store.finish_run(run_id=run_id, status=RunStatus.SUCCEEDED, duration_ms=900)
        await session.commit()

        stored = await session.get(models.Run, run_id)
        assert stored is not None
        assert stored.input_tokens == 500
        assert stored.output_tokens == 100
        assert float(stored.cost_eur) == pytest.approx(0.00125)


async def test_resumable_steps_survive_a_new_session(live_engine) -> None:
    """A run suspended for approval must be resumable from the database, not from memory."""
    factory = async_sessionmaker(live_engine, expire_on_commit=False)
    async with factory() as session:
        agent_name = f"opspilot-{uuid.uuid4().hex[:8]}"
        await _seed_agent(session, agent_name)
        first = PostgresRunStore(session)
        run_id = await first.create_run(agent=agent_name, request={}, model=None)
        step_id = await first.create_step(run_id=run_id, index=0, name="classify")
        await first.finish_step(
            step_id=step_id,
            status=StepStatus.SUCCEEDED,
            summary="done",
            detail={},
            duration_ms=5,
        )
        gate_id = await first.create_step(run_id=run_id, index=1, name="propose_remediation")
        await first.finish_step(
            step_id=gate_id,
            status=StepStatus.WAITING_APPROVAL,
            summary="needs approval",
            detail={"remediation": {"status": "waiting_approval"}},
            duration_ms=7,
        )
        await first.finish_run(run_id=run_id, status=RunStatus.WAITING_APPROVAL, duration_ms=12)
        await session.commit()

    async with factory() as fresh_session:
        second = PostgresRunStore(fresh_session)
        records = await second.steps(run_id)
        # Read inside the session: a session used after its context exits is a bug the warning below
        # used to hide.
        assert [record.status for record in records] == [
            StepStatus.SUCCEEDED,
            StepStatus.WAITING_APPROVAL,
        ]
        assert records[1].detail == {"remediation": {"status": "waiting_approval"}}

    async with factory() as check_session:
        run = await check_session.get(models.Run, run_id)
        assert run is not None and run.status is RunStatus.WAITING_APPROVAL
        waiting = (
            await check_session.execute(
                sa.select(sa.func.count()).select_from(models.RunStep).where(models.RunStep.run_id == run_id)
            )
        ).scalar_one()
        assert waiting == 2
