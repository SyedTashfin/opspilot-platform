"""Cost accounting: the numbers the dashboard is allowed to show.

Two responsibilities, deliberately kept apart:

* ``ModelCallRecorder`` persists one row per model call — the evidence.
* ``CostLedger`` answers "how much has been spent" — the enforcement input.

The in-memory implementations are what unit tests use, so no test needs a database and no test
depends on the wall clock.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.db.models import ModelCall
from opspilot.domain.enums import UNSPECIFIED_STEP, CallStatus
from opspilot.gateway.types import ModelResponse


@dataclass(frozen=True, slots=True)
class ModelCallRecord:
    run_id: uuid.UUID
    step: str
    provider: str
    model: str
    request_id: str
    input_tokens: int
    output_tokens: int
    cost_eur: float
    cost_known: bool
    latency_ms: int
    status: CallStatus
    attempts: int
    fallback_used: bool
    error: str | None = None

    @staticmethod
    def from_response(
        run_id: uuid.UUID, response: ModelResponse, step: str | None = None
    ) -> ModelCallRecord:
        return ModelCallRecord(
            run_id=run_id,
            step=step or response.step,
            provider=response.provider,
            model=response.model,
            request_id=response.request_id,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            # Unknown pricing is stored as 0 for arithmetic but carries cost_known=False, so a
            # dashboard can render "cost unknown" instead of silently under-reporting.
            cost_eur=response.cost_eur or 0.0,
            cost_known=response.cost_known,
            latency_ms=response.latency_ms,
            status=CallStatus.OK,
            attempts=response.attempts,
            fallback_used=response.fallback_used,
        )


class ModelCallRecorder(Protocol):
    async def record(self, call: ModelCallRecord) -> None: ...


class CostLedger(Protocol):
    async def spent_today_eur(self) -> float: ...

    async def spent_in_run_eur(self, run_id: uuid.UUID) -> float: ...


@dataclass
class InMemoryRecorder:
    """Test double; also usable for dry runs where persistence is not wanted."""

    calls: list[ModelCallRecord] = field(default_factory=list)

    async def record(self, call: ModelCallRecord) -> None:
        self.calls.append(call)

    def total_eur(self) -> float:
        return sum(call.cost_eur for call in self.calls)

    def unknown_cost_calls(self) -> list[ModelCallRecord]:
        return [call for call in self.calls if not call.cost_known]


@dataclass
class InMemoryLedger:
    """Test double with an explicitly settable "today" total, so tests are not clock-dependent."""

    by_run: dict[uuid.UUID, float] = field(default_factory=dict)
    today: float = 0.0

    async def spent_today_eur(self) -> float:
        return self.today

    async def spent_in_run_eur(self, run_id: uuid.UUID) -> float:
        return self.by_run.get(run_id, 0.0)


class PostgresRecorder:
    """Persists to ``model_calls``. The session is passed in, so the recorder never holds a
    long-lived connection.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, call: ModelCallRecord) -> None:
        self._session.add(
            ModelCall(
                run_id=call.run_id,
                provider=call.provider,
                model=call.model,
                step=call.step or UNSPECIFIED_STEP,
                request_id=call.request_id,
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                cost_eur=call.cost_eur,
                latency_ms=call.latency_ms,
                status=call.status,
                error=call.error,
            )
        )
        await self._session.flush()


class PostgresLedger:
    """Sums recorded spend for enforcement.

    At demo scale a SUM over ``model_calls`` is cheap. Note that it deliberately does *not* filter
    on ``cost_known``: unknown-cost calls contribute zero, which makes the budget a lower bound —
    the honest behaviour, since we cannot enforce against a number we never computed.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def spent_today_eur(self) -> float:
        start_of_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        result = await self._session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(ModelCall.cost_eur), 0)).where(
                ModelCall.created_at >= start_of_day
            )
        )
        return float(result.scalar_one())

    async def spent_in_run_eur(self, run_id: uuid.UUID) -> float:
        result = await self._session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(ModelCall.cost_eur), 0)).where(
                ModelCall.run_id == run_id
            )
        )
        return float(result.scalar_one())
