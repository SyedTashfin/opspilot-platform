"""Process-wide engine/session factory plus the FastAPI session dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from opspilot.config import Settings, get_settings
from opspilot.db.base import create_engine, create_session_factory

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine
    if _engine is None:
        cfg = settings or get_settings()
        _engine = create_engine(cfg.database_url)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = create_session_factory(get_engine())
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. The session is never long-lived and never shared across requests."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
