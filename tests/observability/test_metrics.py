"""Platform metrics: the arithmetic, with provenance.

These are unit tests over a pure function on purpose. The dashboard's claims and the tests' claims are
then the same code, and neither needs a database to be checked.
"""

from __future__ import annotations

from datetime import UTC, datetime

from opspilot.observability.metrics import (
    PlatformFacts,
    TraceData,
    percentile,
    summarise,
    summarise_trace,
)
from opspilot.observability.spans import SpanRecord


def test_percentile_is_nearest_rank_and_returns_observed_values() -> None:
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert percentile(values, 0.95) == 100.0
    assert percentile(values, 0.5) == 50.0
    assert percentile([7], 0.95) == 7.0
    assert percentile([], 0.95) == 0.0


def test_every_headline_number_carries_its_source_and_basis() -> None:
    overview = summarise(
        PlatformFacts(
            environment="ci",
            runs_by_status={"succeeded": 3, "failed": 1, "waiting_approval": 2},
            spend_total_eur=0.012345,
            spend_today_eur=0.001,
            model_calls_total=4,
            input_tokens=1000,
            output_tokens=250,
            step_latencies_ms=[100, 300],
            run_durations_ms=[500, 1500, 2500, 9000],
            spans_total=42,
            approvals_pending=1,
            scenario_labels=["demo"],
        )
    )

    metrics = overview.model_dump()
    for field in (
        "runs_total",
        "success_rate",
        "spend_today_eur",
        "spend_total_eur",
        "model_calls_total",
        "tokens_total",
        "mean_step_latency_ms",
        "p95_run_duration_ms",
        "spans_total",
        "approvals_pending",
    ):
        assert metrics[field]["source"] == "measured", field
        assert metrics[field]["basis"], f"{field} has no stated basis"


def test_success_rate_counts_only_finished_runs() -> None:
    overview = summarise(
        PlatformFacts(
            environment="ci",
            runs_by_status={"succeeded": 6, "failed": 3, "timeout": 1, "running": 40},
        )
    )

    assert overview.success_rate.value == 0.6
    assert overview.runs_total.value == 50.0


def test_success_rate_with_no_finished_runs_is_zero_not_undefined() -> None:
    overview = summarise(PlatformFacts(environment="ci", runs_by_status={"running": 2}))

    assert overview.success_rate.value == 0.0


def test_averages_and_percentiles_over_empty_input_are_zero() -> None:
    overview = summarise(PlatformFacts(environment="ci"))

    assert overview.mean_step_latency_ms.value == 0.0
    assert overview.p95_run_duration_ms.value == 0.0
    assert overview.tokens_total.value == 0.0


def test_the_overview_says_where_the_telemetry_came_from() -> None:
    overview = summarise(PlatformFacts(environment="ci", scenario_labels=["demo", "live"], spans_total=1))

    assert overview.data_sources == ["demo", "live", "postgres"]


def _span(name: str, *, started_ms: int, duration_ms: int, status: str = "ok", **attrs: object) -> SpanRecord:
    started = datetime.fromtimestamp(started_ms / 1000, tz=UTC)
    ended = datetime.fromtimestamp((started_ms + duration_ms) / 1000, tz=UTC)
    return SpanRecord(
        trace_id="a" * 32,
        span_id=name[:16].ljust(16, "0"),
        parent_span_id=None,
        name=name,
        kind="internal",
        started_at=started,
        ended_at=ended,
        duration_ms=duration_ms,
        status=status,
        attributes=dict(attrs),
    )


def test_trace_summary_reports_the_slowest_span_and_error_count() -> None:
    import uuid

    data = TraceData(
        run_id=uuid.uuid4(),
        trace_id="a" * 32,
        spans=[
            _span("agent.run", started_ms=0, duration_ms=900),
            _span("agent.step.classify", started_ms=10, duration_ms=100),
            _span("gateway.complete", started_ms=20, duration_ms=750, status="error", llm_model="fake"),
        ],
    )

    view = summarise_trace(data)

    assert view.span_count == 3
    assert view.error_count == 1
    assert view.duration_ms == 900
    assert view.slowest_span == "agent.run"
    assert view.spans[0].name == "agent.run"
    assert view.spans[2].attributes["llm_model"] == "fake"


def test_trace_summary_of_an_empty_trace_is_explicit() -> None:
    import uuid

    view = summarise_trace(TraceData(run_id=uuid.uuid4()))

    assert view.span_count == 0
    assert view.trace_id is None
    assert view.slowest_span is None
