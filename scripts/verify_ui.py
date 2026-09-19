"""Click-through verification: drive the API's pages over HTTP and print what came back.

    uv run python scripts/verify_ui.py

Requires PostgreSQL running and migrated (``docker compose up -d postgres && uv run alembic upgrade
head``). The Incident Lab runs in process here, which is the documented test seam, so no second process
is needed; in a real deployment the lab is the compose ``demo-service`` and the API talks to it over HTTP.

This is deliberately a script rather than only a pytest file: it is what a person runs when they want to
see the product answer, and it prints the status codes and page sizes that the automated tests assert on.
"""

from __future__ import annotations

import asyncio

import httpx

from opspilot.api import service
from opspilot.api.main import create_app, seed_agents
from opspilot.lab.service import create_lab_app
from opspilot.observability.tracing import configure_tracing

SCENARIO = "rec-latency-bad-deploy"


async def main() -> int:
    # Both of these normally happen in the app lifespan, which an ASGI transport does not run.
    configure_tracing("opspilot-api", environment="dev")
    service.LAB_TRANSPORT = httpx.ASGITransport(app=create_lab_app())  # type: ignore[assignment]
    await seed_agents()

    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://api",
        follow_redirects=False,
        timeout=120,
    ) as client:
        index = await client.get("/")
        print(f"GET  /                       -> {index.status_code}, {len(index.text)} bytes")

        scenarios = await client.get("/api/v1/scenarios")
        print(f"GET  /api/v1/scenarios       -> {scenarios.status_code}, {len(scenarios.json()['scenarios'])} scenarios")

        posted = await client.post("/runs", data={"scenario_id": SCENARIO, "provider": "fake"})
        location = posted.headers.get("location", "")
        print(f"POST /runs                   -> {posted.status_code} -> {location}")
        if not location:
            print("      body:", posted.text[:300].replace("\n", " "))
            return 1
        run_id = location.rsplit("/", 1)[-1]

        page = await client.get(f"/runs/{run_id}")
        print(f"GET  /runs/{run_id[:8]}      -> {page.status_code}, {len(page.text)} bytes")

        payload = await client.get(f"/api/v1/runs/{run_id}")
        body = payload.json()
        print(f"GET  /api/v1/runs/{run_id[:8]} -> {payload.status_code}, status={body['run_status']}, {len(body['steps'])} steps")

        trace = await client.get(f"/api/v1/runs/{run_id}/trace")
        trace_body = trace.json()
        print(f"GET  .../trace               -> {trace.status_code}, {trace_body.get('span_count')} spans")

        overview = await client.get("/api/v1/platform/overview")
        metrics = overview.json()
        print(
            f"GET  /api/v1/platform/overview -> {overview.status_code}, "
            f"runs={metrics['runs_total']['value']}, sources={metrics['data_sources']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
