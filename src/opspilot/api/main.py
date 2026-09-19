"""Application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from opspilot import __version__
from opspilot.api.routes import health, platform
from opspilot.config import get_settings
from opspilot.db.session import dispose_engine
from opspilot.observability.logging import configure_logging, get_logger
from opspilot.observability.tracing import configure_tracing, shutdown_tracing


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = get_logger(__name__)
    configure_tracing(
        settings.service_name,
        environment=settings.environment,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
        sample_ratio=settings.trace_sample_ratio,
    )
    logger.info(
        "api.startup",
        environment=settings.environment,
        version=__version__,
        otlp_configured=settings.otel_exporter_otlp_endpoint is not None,
    )
    try:
        yield
    finally:
        await dispose_engine()
        shutdown_tracing()
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
    app.include_router(platform.router)
    return app


app = create_app()
