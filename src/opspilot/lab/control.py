"""The platform's write-side client for the lab: inject, clear, restart, read ground truth.

This module exists outside ``opspilot.tools`` on purpose. It holds the admin token and it can change a
service's behaviour, so it is *not* a tool: nothing an agent can call imports it, and a test asserts
that no registered tool's surface mentions ``/admin``. The evaluation harness (M7) and the lab's own
tests are its users.
"""

from __future__ import annotations

from typing import Any

import httpx

from opspilot.lab.scenarios import LabScenario


class LabControlClient:
    """Admin access to a running Incident Lab service."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._timeout = timeout_seconds
        self._transport = transport

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Lab-Admin-Token": self.token}

    @staticmethod
    def _payload(response: httpx.Response) -> dict[str, Any]:
        """Validate the shape once rather than trusting ``json()``'s Any in four places."""
        data: Any = response.json()
        if not isinstance(data, dict):
            msg = f"the lab returned {type(data).__name__}, expected an object"
            raise ValueError(msg)
        return data

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=self._timeout, transport=self._transport)

    async def inject(self, scenario: LabScenario) -> dict[str, Any]:
        body: dict[str, Any] = {"fault": scenario.fault.model_dump(mode="json")}
        if scenario.deployment is not None:
            body["deployment"] = scenario.deployment.model_dump(mode="json")
        async with await self._client() as client:
            response = await client.post("/admin/faults", json=body, headers=self._headers)
            response.raise_for_status()
            return self._payload(response)

    async def clear(self) -> dict[str, Any]:
        async with await self._client() as client:
            response = await client.delete("/admin/faults", headers=self._headers)
            response.raise_for_status()
            return self._payload(response)

    async def restart(self) -> dict[str, Any]:
        async with await self._client() as client:
            response = await client.post("/admin/restart", headers=self._headers)
            response.raise_for_status()
            return self._payload(response)

    async def ground_truth(self) -> dict[str, Any]:
        """The answer key. Only the evaluation harness and tests read this."""
        async with await self._client() as client:
            response = await client.get("/admin/ground-truth", headers=self._headers)
            response.raise_for_status()
            return self._payload(response)

    async def drive_traffic(self, *, requests: int = 1) -> list[int]:
        """Send real requests so the service has observations to be investigated."""
        statuses: list[int] = []
        async with await self._client() as client:
            for _ in range(requests):
                response = await client.post("/work")
                statuses.append(response.status_code)
        return statuses
