"""Application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from opspilot import __version__
from opspilot.api.routes import health, platform, runs
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
    # The shipped agents are seeded on startup: a run needs an agent row, and creating it lazily on first
    # use would make "the agent exists" a side effect of traffic. A missing database must not stop the
    # process — /readyz reports it instead.
    try:
        created = await seed_agents()
        if created:
            logger.info("api.agents_seeded", created=created)
    except Exception as exc:
        logger.warning("api.agents_seed_failed", error=f"{type(exc).__name__}: {exc}")
    try:
        yield
    finally:
        await dispose_engine()
        shutdown_tracing()
        logger.info("api.shutdown")


async def seed_agents() -> int:
    """Register the shipped agents. Idempotent, so it is safe on every startup."""
    from opspilot.agent.store import AgentSeed, ensure_agents
    from opspilot.db.session import get_session_factory

    async with get_session_factory()() as session:
        created = await ensure_agents(
            session,
            [
                AgentSeed(
                    name="opspilot",
                    description="Infrastructure incident investigation and remediation agent",
                    allowed_tools=[
                        "azure.get_metrics",
                        "azure.query_logs",
                        "azure.get_resource_state",
                        "github.get_recent_deployments",
                        "docs.search_runbook",
                        "azure.restart_service",
                        "azure.rollback_deployment",
                    ],
                    model_policy={},
                    evaluation_suite="opspilot-lab",
                )
            ],
        )
        await session.commit()
    return created


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
    app.include_router(runs.router)
    return app


app = create_app()
