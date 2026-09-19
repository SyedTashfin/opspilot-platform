"""Run persistence.

The runtime depends on this protocol rather than on SQLAlchemy, which keeps runtime tests free of a
database and keeps the persistence concerns in one file. The Postgres implementation also refreshes
the run's token and cost summary from ``model_calls``, because that table — not the run row — is the
source of truth for spend.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.db.models import Agent, ModelCall, Run, RunStep, Span
from opspilot.domain.enums import AgentStatus, RunStatus, StepStatus
from opspilot.observability.spans import SpanRecord


class UnknownAgentError(Exception):
    """Raised when a run is requested for an agent that was never registered."""


@dataclass(frozen=True, slots=True)
class StepRecord:
    index: int
    name: str
    status: StepStatus
    summary: str | None = None
    detail: Mapping[str, Any] | None = None
    duration_ms: int | None = None


class RunStore(Protocol):
    async def create_run(
        self,
        *,
        agent: str,
        request: Mapping[str, Any],
        model: str | None,
        trigger: str | None = None,
    ) -> uuid.UUID: ...

    async def create_step(self, *, run_id: uuid.UUID, index: int, name: str) -> uuid.UUID: ...

    async def finish_step(
        self,
        *,
        step_id: uuid.UUID,
        status: StepStatus,
        summary: str,
        detail: Mapping[str, Any],
        duration_ms: int,
    ) -> None: ...

    async def finish_run(
        self,
        *,
        run_id: uuid.UUID,
        status: RunStatus,
        duration_ms: int,
        error: str | None = None,
    ) -> None: ...

    async def steps(self, run_id: uuid.UUID) -> list[StepRecord]: ...

    async def attach_trace(self, *, run_id: uuid.UUID, trace_id: str) -> None:
        """Record which trace the run belongs to, so its spans can be found later."""
        ...

    async def record_spans(self, *, run_id: uuid.UUID, spans: Sequence[SpanRecord]) -> None: ...


@dataclass
class InMemoryRunStore:
    """Test double. Mirrors the Postgres semantics that the runtime depends on, and nothing else."""

    agents: dict[str, uuid.UUID] = field(default_factory=dict)
    runs: dict[uuid.UUID, dict[str, Any]] = field(default_factory=dict)
    records: dict[uuid.UUID, list[StepRecord]] = field(default_factory=dict)
    spans: dict[uuid.UUID, list[SpanRecord]] = field(default_factory=dict)

    def register_agent(self, name: str) -> uuid.UUID:
        agent_id = uuid.uuid4()
        self.agents[name] = agent_id
        return agent_id

    async def create_run(
        self,
        *,
        agent: str,
        request: Mapping[str, Any],
        model: str | None,
        trigger: str | None = None,
    ) -> uuid.UUID:
        if agent not in self.agents:
            raise UnknownAgentError(f"agent {agent!r} is not registered")
        run_id = uuid.uuid4()
        self.runs[run_id] = {
            "agent": agent,
            "request": dict(request),
            "model": model,
            "trigger": trigger,
            "status": RunStatus.RUNNING,
        }
        self.records[run_id] = []
        return run_id

    async def create_step(self, *, run_id: uuid.UUID, index: int, name: str) -> uuid.UUID:
        step_id = uuid.uuid4()
        self.records[run_id].append(StepRecord(index=index, name=name, status=StepStatus.RUNNING))
        return step_id

    async def finish_step(
        self,
        *,
        step_id: uuid.UUID,
        status: StepStatus,
        summary: str,
        detail: Mapping[str, Any],
        duration_ms: int,
    ) -> None:
        for records in self.records.values():
            for position, record in enumerate(records):
                if record.status is StepStatus.RUNNING and record.duration_ms is None:
                    records[position] = StepRecord(
                        index=record.index,
                        name=record.name,
                        status=status,
                        summary=summary,
                        detail=dict(detail),
                        duration_ms=duration_ms,
                    )
                    return

    async def finish_run(
        self,
        *,
        run_id: uuid.UUID,
        status: RunStatus,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        self.runs[run_id].update(status=status, duration_ms=duration_ms, error=error)

    async def steps(self, run_id: uuid.UUID) -> list[StepRecord]:
        return list(self.records.get(run_id, []))

    async def attach_trace(self, *, run_id: uuid.UUID, trace_id: str) -> None:
        self.runs[run_id]["trace_id"] = trace_id

    async def record_spans(self, *, run_id: uuid.UUID, spans: Sequence[SpanRecord]) -> None:
        self.spans.setdefault(run_id, []).extend(spans)

    def status_of(self, run_id: uuid.UUID) -> RunStatus:
        return cast(RunStatus, self.runs[run_id]["status"])


class PostgresRunStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _agent_id(self, name: str) -> uuid.UUID:
        result = await self._session.execute(sa.select(Agent.id).where(Agent.name == name))
        agent_id = result.scalar_one_or_none()
        if agent_id is None:
            raise UnknownAgentError(f"agent {name!r} is not registered")
        return agent_id

    async def create_run(
        self,
        *,
        agent: str,
        request: Mapping[str, Any],
        model: str | None,
        trigger: str | None = None,
    ) -> uuid.UUID:
        run = Run(
            agent_id=await self._agent_id(agent),
            status=RunStatus.RUNNING,
            trigger=trigger,
            request=dict(request),
            model=model,
            started_at=datetime.now(UTC),
        )
        self._session.add(run)
        await self._session.flush()
        return run.id

    async def create_step(self, *, run_id: uuid.UUID, index: int, name: str) -> uuid.UUID:
        step = RunStep(
            run_id=run_id,
            step_index=index,
            name=name,
            status=StepStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        self._session.add(step)
        await self._session.flush()
        return step.id

    async def finish_step(
        self,
        *,
        step_id: uuid.UUID,
        status: StepStatus,
        summary: str,
        detail: Mapping[str, Any],
        duration_ms: int,
    ) -> None:
        step = await self._session.get(RunStep, step_id)
        if step is None:  # pragma: no cover - the row was created earlier in the same run
            raise UnknownAgentError(f"step {step_id} vanished")
        step.status = status
        step.summary = summary
        step.detail = dict(detail)
        step.duration_ms = duration_ms
        step.completed_at = datetime.now(UTC)
        await self._session.flush()

    async def finish_run(
        self,
        *,
        run_id: uuid.UUID,
        status: RunStatus,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        run = await self._session.get(Run, run_id)
        if run is None:  # pragma: no cover
            raise UnknownAgentError(f"run {run_id} vanished")
        run.status = status
        run.duration_ms = duration_ms
        run.error = error
        run.completed_at = datetime.now(UTC)

        # model_calls is the source of truth for spend; the run row carries a summary for listings.
        totals = await self._session.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(ModelCall.input_tokens), 0),
                sa.func.coalesce(sa.func.sum(ModelCall.output_tokens), 0),
                sa.func.coalesce(sa.func.sum(ModelCall.cost_eur), 0),
            ).where(ModelCall.run_id == run_id)
        )
        input_tokens, output_tokens, cost_eur = totals.one()
        run.input_tokens = int(input_tokens)
        run.output_tokens = int(output_tokens)
        run.cost_eur = float(cost_eur)
        await self._session.flush()

    async def steps(self, run_id: uuid.UUID) -> list[StepRecord]:
        result = await self._session.execute(
            sa.select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.step_index)
        )
        return [
            StepRecord(
                index=step.step_index,
                name=step.name,
                status=step.status,
                summary=step.summary,
                detail=step.detail,
                duration_ms=step.duration_ms,
            )
            for step in result.scalars().all()
        ]

    async def attach_trace(self, *, run_id: uuid.UUID, trace_id: str) -> None:
        run = await self._session.get(Run, run_id)
        if run is None:  # pragma: no cover
            raise UnknownAgentError(f"run {run_id} vanished")
        run.trace_id = trace_id
        await self._session.flush()

    async def record_spans(self, *, run_id: uuid.UUID, spans: Sequence[SpanRecord]) -> None:
        for record in spans:
            self._session.add(Span(**record.as_row(run_id)))
        if spans:
            await self._session.flush()


class AgentSeed(BaseModel):
    """Shape used when seeding the two shipped agents."""

    name: str
    description: str
    allowed_tools: list[str]
    model_policy: dict[str, Any] = {}
    evaluation_suite: str | None = None


async def ensure_agents(session: AsyncSession, seeds: list[AgentSeed]) -> int:
    """Insert any missing agents. Idempotent, so it is safe on every startup."""
    created = 0
    for seed in seeds:
        existing = await session.execute(sa.select(Agent.id).where(Agent.name == seed.name))
        if existing.scalar_one_or_none() is not None:
            continue
        session.add(
            Agent(
                name=seed.name,
                description=seed.description,
                status=AgentStatus.ACTIVE,
                allowed_tools=seed.allowed_tools,
                model_policy=seed.model_policy,
                evaluation_suite=seed.evaluation_suite,
                permissions={},
            )
        )
        created += 1
    await session.flush()
    return created
