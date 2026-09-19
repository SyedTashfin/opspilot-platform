"""Integration tests against a real PostgreSQL.

Opt-in: set ``OPSPILOT_TEST_DATABASE_URL`` to a database whose name contains "test". The tests
create and drop the schema, so the guard is deliberate — pointing them at a real database is
refused rather than allowed by accident.
"""

from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

import opspilot.db.models as models
from opspilot.db.base import Base
from opspilot.domain.enums import AgentStatus, CallStatus, RunStatus

pytestmark = pytest.mark.integration

TEST_URL_ENV = "OPSPILOT_TEST_DATABASE_URL"


def _test_url() -> str:
    url = os.environ.get(TEST_URL_ENV, "")
    if not url:
        pytest.skip(f"{TEST_URL_ENV} is not set")
    database_name = url.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in database_name:
        pytest.fail(f"refusing to run destructive tests against database {database_name!r}")
    return url


async def _engine_or_skip() -> AsyncEngine:
    engine = create_async_engine(_test_url())
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"postgres unreachable: {exc}")
    return engine


async def test_schema_roundtrip_and_run_lifecycle() -> None:
    engine = await _engine_or_skip()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
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
            session.add(
                models.RunStep(run_id=run.id, step_index=0, name="classify", status="succeeded")
            )
            await session.commit()

            stored = await session.get(models.Run, run.id)
            assert stored is not None
            assert stored.status == RunStatus.RUNNING
            assert stored.cost_eur == 0

            calls = (
                (
                    await session.execute(
                        sa.select(models.ModelCall).where(models.ModelCall.run_id == run.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(calls) == 1
            assert calls[0].status == CallStatus.OK
    finally:
        await engine.dispose()
