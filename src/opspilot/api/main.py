"""Application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from opspilot import __version__
from opspilot.api.routes import health
from opspilot.config import get_settings
from opspilot.db.session import dispose_engine
from opspilot.observability.logging import configure_logging, get_logger


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = get_logger(__name__)
    logger.info("api.startup", environment=settings.environment, version=__version__)
    try:
        yield
    finally:
        await dispose_engine()
        logger.info("api.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="OpsPilot Platform API",
        summary="Platform for deploying, running, evaluating, observing and securing AI agents.",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.include_router(health.router)
    return app


app = create_app()
