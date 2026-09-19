"""Platform read endpoints: the overview the dashboard shows and the trace of a run.

Both expose measured data only, and every number carries its provenance and basis (ADR-012). The
dependencies are FastAPI dependencies rather than direct construction, so the endpoint contract is
testable without a database and the query layer is testable without HTTP.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException, status

from opspilot.api.deps import SessionDep, SettingsDep
from opspilot.observability.metrics import (
    PlatformFacts,
    PlatformOverview,
    TraceData,
    TraceView,
    summarise_trace,
)
from opspilot.observability.metrics import summarise as summarise_facts
from opspilot.observability.repository import PostgresMetricsRepository, PostgresTraceRepository

router = APIRouter(prefix="/api/v1", tags=["platform"])


class MetricsSource(Protocol):
    async def facts(self, *, environment: str) -> PlatformFacts: ...


class TraceSource(Protocol):
    async def trace(self, run_id: uuid.UUID) -> TraceData: ...


def metrics_source(session: SessionDep) -> MetricsSource:
    return PostgresMetricsRepository(session)


def trace_source(session: SessionDep) -> TraceSource:
    return PostgresTraceRepository(session)


MetricsDep = Annotated[MetricsSource, Depends(metrics_source)]
TraceDep = Annotated[TraceSource, Depends(trace_source)]


@router.get(
    "/platform/overview",
    response_model=PlatformOverview,
    summary="Platform metrics, each labelled with its source and basis",
)
async def platform_overview(settings: SettingsDep, metrics: MetricsDep) -> PlatformOverview:
    facts = await metrics.facts(environment=settings.environment)
    return summarise_facts(facts)


@router.get(
    "/runs/{run_id}/trace",
    response_model=TraceView,
    summary="OpenTelemetry spans recorded for one run",
)
async def run_trace(run_id: uuid.UUID, traces: TraceDep) -> TraceView:
    data = await traces.trace(run_id)
    if not data.spans and data.trace_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no run {run_id} with recorded spans"
        )
    return summarise_trace(data)
