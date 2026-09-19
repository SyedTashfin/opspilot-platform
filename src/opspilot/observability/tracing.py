"""The OpenTelemetry adapter: one tracer for the platform, exported to Phoenix over OTLP and buffered
into PostgreSQL.

Design notes that matter:

* **Tracing is never required for correctness.** With no provider configured the OTel API returns
  non-recording spans, so instrumented code paths run unchanged in tests and in CI.
* **Attributes are facts, not prose.** A span carries the step name, model, provider, tokens, cost,
  cost-known flag, tool name, permission class, outcome status and hashes. It never carries a prompt, a
  completion or private reasoning — traces are readable by anyone with dashboard access.
* **One collector, drained by run.** See ``spans.py`` for why persistence happens at the end of a run.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, StatusCode

from opspilot.observability.spans import COLLECTOR, SpanRecord

TRACER_NAME = "opspilot"

_provider: TracerProvider | None = None


class _CollectorProcessor(SpanProcessor):
    """Feeds finished spans into the process-wide buffer (see ``opspilot.observability.spans``)."""

    def on_start(self, span: Span, parent_context: Any = None) -> None:
        return None

    def on_end(self, span: ReadableSpan) -> None:
        COLLECTOR.add(_to_record(span))

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def _nanos_to_datetime(nanos: int) -> datetime:
    return datetime.fromtimestamp(nanos / 1_000_000_000, tz=UTC)


def _timestamps(span: ReadableSpan) -> tuple[datetime, datetime, int]:
    """Start, end and duration. ``ReadableSpan`` types these as optional; a missing end means "now"."""
    start_ns = span.start_time
    if start_ns is None:  # pragma: no cover - the SDK always sets a start time
        msg = "span without a start time"
        raise ValueError(msg)
    end_ns = span.end_time if span.end_time is not None else start_ns
    return _nanos_to_datetime(start_ns), _nanos_to_datetime(end_ns), int((end_ns - start_ns) / 1_000_000)


def _to_record(span: ReadableSpan) -> SpanRecord:
    context = span.context
    parent = span.parent
    started_at, ended_at, duration_ms = _timestamps(span)
    attributes = {
        key: value
        for key, value in (span.attributes or {}).items()
        if isinstance(value, str | int | float | bool)
    }
    return SpanRecord(
        trace_id=format(context.trace_id, "032x"),
        span_id=format(context.span_id, "016x"),
        parent_span_id=format(parent.span_id, "016x") if parent is not None else None,
        name=span.name,
        kind=span.kind.name.lower(),
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
        status="error" if span.status.status_code is StatusCode.ERROR else "ok",
        attributes=attributes,
    )


def configure_tracing(
    service_name: str = "opspilot-api",
    *,
    environment: str = "local",
    otlp_endpoint: str | None = None,
    sample_ratio: float = 1.0,
) -> TracerProvider:
    """Install the process tracer provider.

    Idempotent per process: OTel only accepts one global provider, so a second call reuses the first and
    says so rather than silently dropping spans.
    """
    global _provider
    if _provider is not None:
        return _provider

    resource = Resource.create(
        {
            "service.name": service_name,
            "deployment.environment": environment,
            "service.version": _version(),
        }
    )
    provider = TracerProvider(resource=resource, sampler=_sampler(sample_ratio))
    provider.add_span_processor(_CollectorProcessor())
    if otlp_endpoint:
        # Exporter failures are the exporter's problem: a trace viewer being down must never fail a run.
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=_traces_endpoint(otlp_endpoint)))
        )
    trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def _sampler(sample_ratio: float) -> Any:
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, TraceIdRatioBased

    if sample_ratio >= 1.0:
        return ParentBased(ALWAYS_ON)
    return ParentBased(TraceIdRatioBased(sample_ratio))


def _traces_endpoint(base: str) -> str:
    base = base.rstrip("/")
    return base if base.endswith("/v1/traces") else f"{base}/v1/traces"


def _version() -> str:
    from opspilot import __version__

    return __version__


def provider() -> TracerProvider | None:
    return _provider


def shutdown_tracing() -> None:
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


def current_trace_id() -> str | None:
    span = trace.get_current_span()
    context = span.get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x")


def current_span() -> Span:
    return trace.get_current_span()


@contextmanager
def span(name: str, attributes: Mapping[str, Any] | None = None, **extra: Any) -> Iterator[Span]:
    """Start a current span. Child spans started inside it are linked automatically."""
    merged: dict[str, Any] = {**(attributes or {}), **extra}
    with tracer().start_as_current_span(name, attributes=merged) as active:
        yield active


def set_attributes(target: Span, attributes: Mapping[str, Any]) -> None:
    if not target.is_recording():
        return
    for key, value in attributes.items():
        if isinstance(value, str | int | float | bool):
            target.set_attribute(key, value)


def record_error(target: Span, message: str, error_type: str) -> None:
    if not target.is_recording():
        return
    target.set_attribute("error.type", error_type)
    target.set_attribute("error.message", message[:500])
    target.set_status(StatusCode.ERROR, message[:200])


class OtelStepTracer:
    """``AgentRuntime``'s tracing hook: one span per step, parented to the run span."""

    def step_span(
        self,
        *,
        name: str,
        run_id: Any,
        index: int,
        attributes: Mapping[str, Any] | None = None,
    ) -> Any:
        return span(
            f"agent.step.{name}",
            **{
                "agent.step": name,
                "agent.step.index": index,
                "run.id": str(run_id),
                **(dict(attributes) if attributes else {}),
            },
        )
