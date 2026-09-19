"""Integration tests against a real PostgreSQL.

Opt-in: set ``OPSPILOT_TEST_DATABASE_URL`` to a database whose name contains "test". The tests
create and drop the schema, so the guard is deliberate — pointing them at a real database is
refused rather than allowed by accident.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import opspilot.db.models as models
from opspilot.domain.enums import AgentStatus, CallStatus, RunStatus

pytestmark = pytest.mark.integration


async def test_schema_roundtrip_and_run_lifecycle(live_engine: AsyncEngine) -> None:
    session_factory = async_sessionmaker(live_engine, expire_on_commit=False)
    async with session_factory() as session:
        agent = models.Agent(
            name=f"opspilot-{uuid.uuid4().hex[:8]}",
            description="integration test agent",
            status=AgentStatus.ACTIVE,
            allowed_tools=["azure.query_logs"],
        )
        session.add(agent)
        await session.flush()

        run = models.Run(agent_id=agent.id, status=RunStatus.RUNNING, trigger="test")
        session.add(run)
        await session.flush()

        session.add(models.ModelCall(run_id=run.id, provider="fake", model="fake-1"))
        session.add(models.RunStep(run_id=run.id, step_index=0, name="classify", status="succeeded"))
        await session.commit()

        stored = await session.get(models.Run, run.id)
        assert stored is not None
        assert stored.status == RunStatus.RUNNING
        assert stored.cost_eur == 0

        calls = (
            (await session.execute(sa.select(models.ModelCall).where(models.ModelCall.run_id == run.id)))
            .scalars()
            .all()
        )
        assert len(calls) == 1
        assert calls[0].status == CallStatus.OK
