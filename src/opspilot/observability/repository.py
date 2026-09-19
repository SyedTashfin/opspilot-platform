"""Read-side repositories for the observability endpoints.

Reads are deliberately separate from the write path: these queries never mutate, they are the only
place that knows the aggregate SQL, and every aggregate they produce is the input to a
``MetricValue`` whose ``basis`` names the query.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.db.models import Approval, ModelCall, Run, RunStep, Span
from opspilot.domain.enums import ApprovalDecision, RunStatus
from opspilot.observability.metrics import PlatformFacts, TraceData
from opspilot.observability.spans import SpanRecord

TERMINAL_STATUSES = (
    RunStatus.SUCCEEDED.value,
    RunStatus.FAILED.value,
    RunStatus.TIMEOUT.value,
    RunStatus.BUDGET_EXCEEDED.value,
    RunStatus.CANCELLED.value,
)


class PostgresMetricsRepository:
    """Aggregates for the platform overview."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def facts(self, *, environment: str) -> PlatformFacts:
        start_of_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

        runs_by_status = {
            str(row.status): int(row.total)
            for row in (
                await self._session.execute(
                    sa.select(Run.status, sa.func.count().label("total")).group_by(Run.status)
                )
            ).all()
        }
        spend = (
            await self._session.execute(
                sa.select(
                    sa.func.coalesce(sa.func.sum(ModelCall.cost_eur), 0).label("total"),
                    sa.func.coalesce(
                        sa.func.sum(
                            sa.case((ModelCall.created_at >= start_of_day, ModelCall.cost_eur), else_=0)
                        ),
                        0,
                    ).label("today"),
                    sa.func.count().label("calls"),
                    sa.func.coalesce(sa.func.sum(ModelCall.input_tokens), 0).label("input_tokens"),
                    sa.func.coalesce(sa.func.sum(ModelCall.output_tokens), 0).label("output_tokens"),
                )
            )
        ).one()
        step_latencies = [
            int(row)
            for row in (
                await self._session.execute(
                    sa.select(RunStep.duration_ms).where(RunStep.duration_ms.is_not(None))
                )
            )
            .scalars()
            .all()
            if row is not None
        ]
        run_durations = [
            int(row)
            for row in (
                await self._session.execute(
                    sa.select(Run.duration_ms).where(
                        Run.duration_ms.is_not(None), Run.status.in_(TERMINAL_STATUSES)
                    )
                )
            )
            .scalars()
            .all()
            if row is not None
        ]
        spans_total = int(
            (await self._session.execute(sa.select(sa.func.count()).select_from(Span))).scalar_one()
        )
        approvals_pending = int(
            (
                await self._session.execute(
                    sa.select(sa.func.count())
                    .select_from(Approval)
                    .where(Approval.decision == ApprovalDecision.PENDING)
                )
            ).scalar_one()
        )
        # Provenance of the telemetry behind completed runs, so the dashboard can say out loud that the
        # infrastructure data came from the incident lab rather than from live infrastructure.
        # as_string() compiles to ->> on PostgreSQL and works on the portable JSON variant; the old
        # astext() spelling was removed in SQLAlchemy 2.0.
        source_field = Span.attributes["source"].as_string()
        scenario_labels = [
            str(label)
            for label in (
                await self._session.execute(
                    sa.select(sa.distinct(source_field)).where(source_field.is_not(None))
                )
            )
            .scalars()
            .all()
        ]

        return PlatformFacts(
            environment=environment,
            runs_by_status=runs_by_status,
            spend_today_eur=float(spend.today),
            spend_total_eur=float(spend.total),
            model_calls_total=int(spend.calls),
            input_tokens=int(spend.input_tokens),
            output_tokens=int(spend.output_tokens),
            step_latencies_ms=step_latencies,
            run_durations_ms=run_durations,
            spans_total=spans_total,
            approvals_pending=approvals_pending,
            scenario_labels=scenario_labels,
        )


class PostgresTraceRepository:
    """Spans for one run, plus the trace they belong to."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def trace(self, run_id: uuid.UUID) -> TraceData:
        trace_id = (
            await self._session.execute(sa.select(Run.trace_id).where(Run.id == run_id))
        ).scalar_one_or_none()
        rows = (
            (
                await self._session.execute(
                    sa.select(Span).where(Span.run_id == run_id).order_by(Span.started_at)
                )
            )
            .scalars()
            .all()
        )
        return TraceData(
            run_id=run_id,
            trace_id=trace_id,
            spans=[_to_record(row) for row in rows],
        )


def _to_record(row: Span) -> SpanRecord:
    attributes: dict[str, Any] = dict(row.attributes or {})
    return SpanRecord(
        trace_id=row.trace_id,
        span_id=row.span_id,
        parent_span_id=row.parent_span_id,
        name=row.name,
        kind=row.kind,
        started_at=row.started_at,
        ended_at=row.ended_at,
        duration_ms=row.duration_ms,
        status=row.status,
        attributes=attributes,
    )
