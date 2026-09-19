from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from opspilot import __version__
from opspilot.api.main import create_app


async def _get(path: str) -> tuple[int, dict[str, object]]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(path)
    return response.status_code, response.json()


async def test_healthz_never_touches_the_database() -> None:
    status_code, body = await _get("/healthz")
    assert status_code == 200
    assert body == {"status": "ok", "environment": "ci", "version": __version__}


async def test_version_reports_the_service() -> None:
    status_code, body = await _get("/version")
    assert status_code == 200
    assert body["service"] == "opspilot-api"
    assert body["version"] == __version__


async def test_openapi_exposes_the_system_routes() -> None:
    app = create_app()
    schema = app.openapi()
    assert {"/healthz", "/readyz", "/version"} <= set(schema["paths"])
    assert schema["info"]["title"] == "OpsPilot Platform API"
