"""Platform metrics, with the provenance of every number.

Rule this module exists to enforce (ADR-012): a figure shown to a person carries *what it is measured
from* and *how*. A number whose provenance is unknown does not get displayed, and a headline number
whose inputs are simulated is labelled as such rather than quietly mixed with measured ones.

Aggregation is a pure function of the facts read from the database, so the arithmetic is unit-testable
without a database and the claims in the dashboard are the same arithmetic the tests cover.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from opspilot.observability.spans import SpanRecord

SourceLabel = Literal["measured", "seeded", "simulated"]


class MetricValue(BaseModel):
    """A number, its unit, where it came from and how it was computed."""

    value: float
    unit: str
    source: SourceLabel
    basis: str

    @staticmethod
    def measured(value: float, unit: str, basis: str) -> MetricValue:
        return MetricValue(value=value, unit=unit, source="measured", basis=basis)


class PlatformOverview(BaseModel):
    environment: str
    generated_at: datetime
    runs_total: MetricValue
    runs_by_status: dict[str, int]
    success_rate: MetricValue
    spend_today_eur: MetricValue
    spend_total_eur: MetricValue
    model_calls_total: MetricValue
    tokens_total: MetricValue
    mean_step_latency_ms: MetricValue
    p95_run_duration_ms: MetricValue
    spans_total: MetricValue
    approvals_pending: MetricValue
    data_sources: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PlatformFacts:
    """Raw aggregates read from the database. Inputs to the summary, not presentation."""

    environment: str
    runs_by_status: dict[str, int] = field(default_factory=dict)
    spend_today_eur: float = 0.0
    spend_total_eur: float = 0.0
    model_calls_total: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    step_latencies_ms: list[int] = field(default_factory=list)
    run_durations_ms: list[int] = field(default_factory=list)
    spans_total: int = 0
    approvals_pending: int = 0
    scenario_labels: list[str] = field(default_factory=list)

    def total_runs(self) -> int:
        return sum(self.runs_by_status.values())


def percentile(values: list[int], fraction: float) -> float:
    """Nearest-rank percentile.

    Chosen over interpolation because it always returns a value that was actually observed, and a
    latency p95 that no request had is a small lie that compounds.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, round(fraction * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def summarise(facts: PlatformFacts) -> PlatformOverview:
    runs_total = facts.total_runs()
    succeeded = facts.runs_by_status.get("succeeded", 0)
    finished = sum(
        count
        for status, count in facts.runs_by_status.items()
        if status in {"succeeded", "failed", "timeout", "budget_exceeded", "cancelled"}
    )
    success_rate = (succeeded / finished) if finished else 0.0
    mean_step_latency = (
        sum(facts.step_latencies_ms) / len(facts.step_latencies_ms) if facts.step_latencies_ms else 0.0
    )
    sources = sorted(set(facts.scenario_labels))
    sources.append("postgres")
    return PlatformOverview(
        environment=facts.environment,
        generated_at=datetime.now(UTC),
        runs_total=MetricValue.measured(
            float(runs_total), "runs", "count(runs) where the row exists in this environment"
        ),
        runs_by_status=dict(sorted(facts.runs_by_status.items())),
        success_rate=MetricValue.measured(
            round(success_rate, 4),
            "ratio",
            "count(runs.status='succeeded') / count(runs whose status is terminal)",
        ),
        spend_today_eur=MetricValue.measured(
            round(facts.spend_today_eur, 6),
            "EUR",
            "sum(model_calls.cost_eur) where created_at >= start of day; unknown-price calls contribute 0",
        ),
        spend_total_eur=MetricValue.measured(
            round(facts.spend_total_eur, 6),
            "EUR",
            "sum(model_calls.cost_eur) over all recorded calls",
        ),
        model_calls_total=MetricValue.measured(float(facts.model_calls_total), "calls", "count(model_calls)"),
        tokens_total=MetricValue.measured(
            float(facts.input_tokens + facts.output_tokens),
            "tokens",
            "sum(model_calls.input_tokens + model_calls.output_tokens), provider-reported",
        ),
        mean_step_latency_ms=MetricValue.measured(
            round(mean_step_latency, 1), "ms", "mean(run_steps.duration_ms) over executed steps"
        ),
        p95_run_duration_ms=MetricValue.measured(
            percentile(facts.run_durations_ms, 0.95),
            "ms",
            "nearest-rank p95 of runs.duration_ms over completed runs",
        ),
        spans_total=MetricValue.measured(float(facts.spans_total), "spans", "count(spans)"),
        approvals_pending=MetricValue.measured(
            float(facts.approvals_pending),
            "approvals",
            "count(approvals) where decision='pending'",
        ),
        # Where the telemetry behind these runs came from: no number here is presented as live
        # infrastructure data unless a live source produced it.
        data_sources=sorted(set(sources)),
    )


class SpanView(BaseModel):
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    status: str
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    attributes: dict[str, object] = Field(default_factory=dict)


class TraceView(BaseModel):
    run_id: uuid.UUID
    trace_id: str | None
    span_count: int
    error_count: int
    duration_ms: int
    slowest_span: str | None
    spans: list[SpanView] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TraceData:
    run_id: uuid.UUID
    trace_id: str | None = None
    spans: list[SpanRecord] = field(default_factory=list)


def summarise_trace(data: TraceData) -> TraceView:
    """Shape a run's spans for reading. Spans are ordered as they started, parents before children."""
    ordered = sorted(data.spans, key=lambda record: (record.started_at, record.duration_ms * -1))
    slowest = max(ordered, key=lambda record: record.duration_ms, default=None)
    return TraceView(
        run_id=data.run_id,
        trace_id=data.trace_id,
        span_count=len(ordered),
        error_count=sum(1 for record in ordered if record.status == "error"),
        duration_ms=max((record.duration_ms for record in ordered), default=0),
        slowest_span=slowest.name if slowest is not None else None,
        spans=[
            SpanView(
                span_id=record.span_id,
                parent_span_id=record.parent_span_id,
                name=record.name,
                kind=record.kind,
                status=record.status,
                started_at=record.started_at,
                ended_at=record.ended_at,
                duration_ms=record.duration_ms,
                attributes=dict(record.attributes),
            )
            for record in ordered
        ],
    )
