"""Shared fixtures for tests that need a real PostgreSQL.

Opt-in and destructive by design: the tests create and drop the schema, so pointing them at a
database whose name does not contain "test" is refused rather than allowed by accident.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from opspilot.db.base import Base

TEST_URL_ENV = "OPSPILOT_TEST_DATABASE_URL"


def test_url() -> str:
    url = os.environ.get(TEST_URL_ENV, "")
    if not url:
        pytest.skip(f"{TEST_URL_ENV} is not set")
    database_name = url.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in database_name:
        pytest.fail(f"refusing to run destructive tests against database {database_name!r}")
    return url


@pytest.fixture
async def live_engine() -> AsyncIterator[AsyncEngine]:
    """An engine connected to a live PostgreSQL with an empty, freshly created schema."""
    engine = create_async_engine(test_url())
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"postgres unreachable: {exc}")
    try:
        async with engine.begin() as connection:
            # Without this, a single aborted transaction left open by a failing test blocks DROP TABLE
            # and the whole suite waits forever instead of reporting the failure.
            await connection.execute(sa.text("SET lock_timeout = '5s'"))
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def live_session(live_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(live_engine, expire_on_commit=False)
    async with factory() as session:
        yield session
