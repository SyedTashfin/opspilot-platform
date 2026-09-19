"""Liveness, readiness and version.

``/healthz`` must never touch the database: a liveness probe that fails when Postgres is down makes
an outage worse by restarting healthy containers.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from opspilot import __version__
from opspilot.api.deps import SessionDep, SettingsDep
from opspilot.observability.logging import get_logger

router = APIRouter(tags=["system"])
logger = get_logger(__name__)


@router.get("/healthz", summary="Liveness")
async def healthz(settings: SettingsDep) -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment, "version": __version__}


@router.get("/readyz", summary="Readiness (checks the database)")
async def readyz(response: Response, session: SessionDep) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("readiness.database_unreachable", error=str(exc))
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "unreachable"}
    return {"status": "ready", "database": "ok"}


@router.get("/version", summary="Build version")
async def version() -> dict[str, str]:
    return {"service": "opspilot-api", "version": __version__}
