"""The surfaces a visitor can click.

Two audiences, deliberately separated:

* **JSON** (`/api/v1/...`) is the contract a Next.js app or any client would consume: list scenarios,
  start a run, read a run, read its trace, read the overview.
* **HTML** (`/`, `/runs/{id}`) is server-rendered so that a reviewer with a browser and no build step can
  watch an investigation happen and read what it concluded. Next.js remains the intended UI (ADR-001);
  this exists because a platform whose only interface is a JSON schema is not a thing anyone can evaluate
  in ninety seconds.

The HTML renderer escapes every value it prints. That is not boilerplate here: evidence contains log
lines and runbook text, and one of the shipped scenarios deliberately embeds instructions in retrieved
data, so a page that interpolated evidence unescaped would be an injection vector in the demo itself.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.agent.store import UnknownAgentError
from opspilot.agents.opspilot.pipeline import report_from_steps
from opspilot.api.deps import SessionDep, SettingsDep
from opspilot.api.service import build_investigation, lab_control
from opspilot.api.views import render_index, render_run
from opspilot.domain.enums import RunStatus
from opspilot.lab.scenarios import SCENARIOS
from opspilot.observability.logging import get_logger

router = APIRouter(tags=["runs"])
logger = get_logger(__name__)

#: The agent the platform runs. Registered at startup by the seed (see db/seed or the lifespan).
AGENT_NAME = "opspilot"


class StartRunRequest(BaseModel):
    scenario_id: str = Field(description="Which incident to investigate")
    provider: str = Field(
        default="configured",
        description="'configured' uses the model policy; 'fake' is deterministic and free",
    )


class StartedRun(BaseModel):
    run_id: uuid.UUID
    scenario_id: str
    service: str
    status: str
    provider: str
    model: str
    message: str


def scenario_catalogue() -> list[dict[str, Any]]:
    """What the agent may be pointed at. Deliberately without the answer key: root causes, acceptable
    diagnoses and expected tools stay in the lab's admin surface and the evaluation dataset."""
    return [
        {
            "scenario_id": scenario.scenario_id,
            "title": scenario.title,
            "service": scenario.service,
            "symptom": scenario.alert_symptom,
            "window_minutes": scenario.window_minutes,
        }
        for scenario in SCENARIOS.values()
    ]


async def _start(session: AsyncSession, settings: Any, body: StartRunRequest) -> StartedRun:
    if body.scenario_id not in SCENARIOS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown scenario {body.scenario_id!r}",
        )
    scenario = SCENARIOS[body.scenario_id]
    investigation = build_investigation(session, settings, provider_label=body.provider)

    # Arm the lab before investigating it: an investigation of a healthy service is a valid run and a
    # useless demo, so the scenario's fault is injected here rather than assumed to be already running.
    control = lab_control(settings)
    await control.inject(scenario)
    await control.drive_traffic(requests=3)

    try:
        summary = await investigation.runtime.execute(
            agent=AGENT_NAME, request=scenario.alert(), steps=investigation.steps()
        )
    except UnknownAgentError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"agent {AGENT_NAME!r} is not registered: {exc}",
        ) from exc
    await session.commit()

    return StartedRun(
        run_id=summary.run_id,
        scenario_id=scenario.scenario_id,
        service=scenario.service,
        status=summary.status.value,
        provider=investigation.provider_label,
        model=investigation.model,
        message=f"{summary.steps_executed} steps in {summary.duration_ms}ms",
    )


@router.get("/api/v1/scenarios", summary="Incidents the agent can be pointed at")
async def list_scenarios() -> dict[str, Any]:
    return {"scenarios": scenario_catalogue()}


@router.post("/api/v1/runs", response_model=StartedRun, summary="Investigate an incident")
async def start_run(
    session: SessionDep,
    settings: SettingsDep,
    body: Annotated[StartRunRequest, Body()],
) -> StartedRun:
    return await _start(session, settings, body)


@router.get("/api/v1/runs/{run_id}", summary="A run: status, steps and report")
async def read_run(run_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    from opspilot.agent.store import PostgresRunStore

    store = PostgresRunStore(session)
    try:
        records = await store.steps(run_id)
    except UnknownAgentError as exc:  # pragma: no cover - run vanished mid-read
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if not records:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no run {run_id}")
    report = report_from_steps(records)
    return {
        "run_id": str(run_id),
        "run_status": await store.status_of(run_id),
        "steps": [
            {
                "index": record.index,
                "name": record.name,
                "status": record.status.value,
                "summary": record.summary,
                "duration_ms": record.duration_ms,
                "detail": record.detail or {},
            }
            for record in records
        ],
        "report": report.model_dump(mode="json") if report is not None else None,
    }


# --- the clickable surface ----------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(session: SessionDep, settings: SettingsDep) -> HTMLResponse:
    from opspilot.observability.metrics import summarise
    from opspilot.observability.repository import PostgresMetricsRepository

    facts = await PostgresMetricsRepository(session).facts(environment=settings.environment)
    overview = summarise(facts)
    recent = await _recent_runs(session)
    return HTMLResponse(
        render_index(
            overview=overview.model_dump(mode="json"),
            scenarios=scenario_catalogue(),
            recent=recent,
            lab_url=settings.lab_base_url,
            provider_label="configured" if _configured(settings) else "fake",
        )
    )


@router.post("/runs", response_class=RedirectResponse, include_in_schema=False)
async def start_run_from_page(
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
) -> RedirectResponse:
    form = await request.form()
    scenario_id = str(form.get("scenario_id", ""))
    provider = str(form.get("provider", "configured"))
    started = await _start(session, settings, StartRunRequest(scenario_id=scenario_id, provider=provider))
    return RedirectResponse(url=f"/runs/{started.run_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/runs/{run_id}", response_class=HTMLResponse, include_in_schema=False)
async def run_page(run_id: uuid.UUID, session: SessionDep) -> HTMLResponse:
    from opspilot.agent.store import PostgresRunStore
    from opspilot.observability.metrics import summarise_trace
    from opspilot.observability.repository import PostgresTraceRepository

    store = PostgresRunStore(session)
    records = await store.steps(run_id)
    if not records:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no run {run_id}")
    trace = summarise_trace(await PostgresTraceRepository(session).trace(run_id))
    report = report_from_steps(records)
    return HTMLResponse(
        render_run(
            run_id=str(run_id),
            status=await store.status_of(run_id),
            steps=[
                {
                    "index": record.index,
                    "name": record.name,
                    "status": record.status.value,
                    "summary": record.summary,
                    "duration_ms": record.duration_ms,
                }
                for record in records
            ],
            report=report.model_dump(mode="json") if report is not None else None,
            trace=trace.model_dump(mode="json"),
        )
    )


async def _recent_runs(session: AsyncSession, *, limit: int = 12) -> list[dict[str, Any]]:
    import sqlalchemy as sa

    from opspilot.db.models import Run

    rows = (
        (await session.execute(sa.select(Run).order_by(Run.started_at.desc()).limit(limit))).scalars().all()
    )
    return [
        {
            "run_id": str(run.id),
            "status": run.status.value if isinstance(run.status, RunStatus) else str(run.status),
            "trigger": run.trigger,
            "model": run.model,
            "duration_ms": run.duration_ms,
            "cost_eur": float(run.cost_eur or 0.0),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "trace_id": run.trace_id,
        }
        for run in rows
    ]


def _configured(settings: Any) -> bool:
    from opspilot.api.service import has_provider_key

    return has_provider_key(settings)
