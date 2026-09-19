"""The demo service: a real HTTP service, plus the control API that makes it misbehave.

Three surfaces, deliberately separated:

* **the service** (`/work`, `/healthz`) — what a real caller hits. It behaves according to the injected
  fault, and it really does slow down or fail.
* **the read surface** (`/metrics`, `/logs`, `/deployments`, `/state`) — what the platform's telemetry
  tools read. It speaks the platform's telemetry shapes, so the HTTP adapter is thin.
* **the admin surface** (`/admin/...`) — injection, restart and the withheld ground truth. It requires a
  token, and **no agent tool points at it**: an agent must not be able to read the answer key or inject
  its own fault. A test asserts that no registered tool URL touches `/admin`.

Available on the platform API's host only (bound to the compose network), and treated as an incident
target rather than as part of the platform.
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from opspilot.lab.faults import (
    WINDOW_MINUTES,
    DeploymentChange,
    FaultConfig,
    FaultKind,
    LabState,
)

# Not a credential: a development default for the lab's control API. A deployment sets
# OPSPILOT_LAB_ADMIN_TOKEN, and no agent tool points at that surface anyway.
DEFAULT_ADMIN_TOKEN = "local-lab-admin"  # noqa: S105
BASE_LATENCY_MS = 45.0


class InjectionRequest(BaseModel):
    fault: FaultConfig
    deployment: DeploymentChange | None = None


class InjectionResponse(BaseModel):
    scenario_id: str
    service: str
    fault: FaultConfig
    injected_at: str | None
    deployment: DeploymentChange | None = None


def create_lab_app(
    *,
    state: LabState | None = None,
    admin_token: str = DEFAULT_ADMIN_TOKEN,
    service_name: str = "recommendation-service",
    seed_backlog: bool = True,
) -> FastAPI:
    lab = state or LabState(service=service_name)
    if seed_backlog and not lab.observations:
        lab.seed_backlog()
        lab.deployments.append(
            DeploymentChange(
                version="rec-2026.05.9",
                changes=("dependency client timeout unchanged at 2000ms", "cache warmup off"),
            )
        )
        lab.log("INFO", "service started", version="rec-2026.05.9")

    app = FastAPI(title="OpsPilot Incident Lab — demo service", version="1.0.0")
    service_router = APIRouter(tags=["service"])
    read_router = APIRouter(tags=["telemetry"])
    admin_router = APIRouter(prefix="/admin", tags=["admin"])

    def require_admin(token: str | None) -> None:
        if token != admin_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="the lab control API requires the admin token",
            )

    @service_router.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "service": lab.service, "fault_active": lab.fault.active}

    @service_router.post("/work")
    async def work() -> dict[str, Any]:
        """Serve one request, behaving the way the injected fault dictates."""
        fault = lab.fault
        latency = BASE_LATENCY_MS
        if fault.kind is FaultKind.LATENCY:
            latency += fault.latency_ms
        elif fault.kind is FaultKind.DEPENDENCY_TIMEOUT:
            latency += fault.dependency_timeout_ms * max(1, fault.dependency_retries)
        elif fault.kind is FaultKind.CPU_SATURATION:
            latency += fault.cpu_percent * 3.5

        jitter = random.uniform(-0.08, 0.08)  # noqa: S311 - load jitter, not a security decision
        latency = max(1.0, latency * (1 + jitter))

        # The status is decided before the request is recorded, so exactly one observation describes
        # this request.
        rate = fault.error_rate
        if fault.kind is FaultKind.ERRORS and rate == 0.0:
            rate = 1.0
        failed = rate > 0 and random.random() < rate  # noqa: S311 - simulated failure rate
        await asyncio.sleep(min(latency, 1_500) / 1_000)
        lab.observe(latency_ms=latency, status=503 if failed else 200)

        if failed:
            lab.log(
                "ERROR",
                "upstream dependency unavailable: returning 503",
                request_id=lab.new_request_id(),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="upstream dependency unavailable",
            )

        if fault.kind is FaultKind.DEPENDENCY_TIMEOUT:
            lab.log(
                "WARN",
                f"feature-store request timed out after {fault.dependency_timeout_ms}ms, "
                f"retrying (attempt {fault.dependency_retries})",
                dependency="feature-store",
            )
        return {
            "service": lab.service,
            "request_id": lab.new_request_id(),
            "latency_ms": round(latency, 2),
            "status": 200,
        }

    @read_router.get("/metrics")
    async def metrics(window_minutes: int = WINDOW_MINUTES) -> dict[str, Any]:
        return {
            "service": lab.service,
            "window_minutes": window_minutes,
            "source": "demo",
            "series": lab.metric_series(minutes=window_minutes),
        }

    @read_router.get("/logs")
    async def logs(
        window_minutes: int = WINDOW_MINUTES,
        level: str | None = None,
        contains: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        lines = lab.recent_logs(window_minutes=window_minutes, level=level, contains=contains, limit=limit)
        return {
            "service": lab.service,
            "window_minutes": window_minutes,
            "source": "demo",
            "lines": [
                {
                    "at": line.at.isoformat(),
                    "level": line.level,
                    "message": line.message,
                    "attributes": line.attributes,
                }
                for line in lines
            ],
        }

    @read_router.get("/deployments")
    async def deployments(limit: int = 5) -> dict[str, Any]:
        ordered = sorted(lab.deployments, key=lambda change: change.at, reverse=True)[:limit]
        return {
            "service": lab.service,
            "source": "demo",
            "deployments": [change.model_dump(mode="json") for change in ordered],
        }

    @read_router.get("/state")
    async def resource_state() -> dict[str, Any]:
        return lab.resource_state()

    @admin_router.get("/faults")
    async def read_faults(
        x_lab_admin_token: Annotated[str | None, Header()] = None,
    ) -> InjectionResponse:
        require_admin(x_lab_admin_token)
        return InjectionResponse(
            scenario_id=lab.scenario_id,
            service=lab.service,
            fault=lab.fault,
            injected_at=lab.injected_at.isoformat() if lab.injected_at else None,
            deployment=lab.deployments[-1] if lab.deployments else None,
        )

    @admin_router.post("/faults")
    async def inject(
        payload: InjectionRequest,
        x_lab_admin_token: Annotated[str | None, Header()] = None,
    ) -> InjectionResponse:
        require_admin(x_lab_admin_token)
        lab.inject(payload.fault, deployment=payload.deployment)
        return InjectionResponse(
            scenario_id=lab.scenario_id,
            service=lab.service,
            fault=lab.fault,
            injected_at=lab.injected_at.isoformat() if lab.injected_at else None,
            deployment=payload.deployment,
        )

    @admin_router.delete("/faults")
    async def clear(x_lab_admin_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        require_admin(x_lab_admin_token)
        lab.clear()
        return {"cleared": True, "service": lab.service}

    @admin_router.post("/restart")
    async def restart(x_lab_admin_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        require_admin(x_lab_admin_token)
        lab.record_restart()
        return {"restarted": True, "restarts_last_hour": lab.restarts, "service": lab.service}

    @admin_router.get("/ground-truth")
    async def ground_truth(x_lab_admin_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        require_admin(x_lab_admin_token)
        return lab.ground_truth()

    app.include_router(service_router)
    app.include_router(read_router)
    app.include_router(admin_router)
    app.state.lab = lab
    return app


#: Module-level app for `uvicorn opspilot.lab.service:app`. The token is read from the environment so
#: a deployment does not ship the development default.
app = create_lab_app(admin_token=os.environ.get("OPSPILOT_LAB_ADMIN_TOKEN", DEFAULT_ADMIN_TOKEN))
