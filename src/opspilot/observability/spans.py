"""Span data and the in-process buffer that holds finished spans until a run can persist them.

This module holds no OpenTelemetry import on purpose: span *records* are plain data, so the run store
can persist them and tests can build them without a tracing stack. The OTel adapter lives in
``tracing.py``.

Why buffer rather than write from a span processor: a span processor callback is synchronous and runs
on whatever thread finished the span, so writing to PostgreSQL from there would mean a second writer,
a second connection and a second failure mode, mid-run. Instead the runtime drains the spans belonging
to its own trace when the run ends and persists them in the run's session — one writer, and spans for a
failed run are still written because the drain happens in the same code path that finishes the run.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class SpanRecord:
    """One finished span. Attributes are primitives so the row is JSON-serialisable."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    status: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def as_row(self, run_id: uuid.UUID | None) -> dict[str, Any]:
        return {
            "id": uuid.uuid4(),
            "run_id": run_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "attributes": self.attributes,
        }


class SpanCollector:
    """Thread-safe buffer of finished spans, drained per trace."""

    def __init__(self, *, max_spans: int = 20_000) -> None:
        self._spans: list[SpanRecord] = []
        self._lock = threading.Lock()
        self._max_spans = max_spans
        self.dropped = 0

    def add(self, record: SpanRecord) -> None:
        with self._lock:
            if len(self._spans) >= self._max_spans:
                self.dropped += 1
                return
            self._spans.append(record)

    def drain(self, *, trace_id: str | None = None) -> list[SpanRecord]:
        """Remove and return buffered spans, optionally only those of one trace."""
        with self._lock:
            if trace_id is None:
                drained, self._spans = self._spans, []
                return drained
            drained = [record for record in self._spans if record.trace_id == trace_id]
            self._spans = [record for record in self._spans if record.trace_id != trace_id]
            return drained

    def peek(self, *, trace_id: str | None = None) -> list[SpanRecord]:
        with self._lock:
            if trace_id is None:
                return list(self._spans)
            return [record for record in self._spans if record.trace_id == trace_id]

    def __len__(self) -> int:
        with self._lock:
            return len(self._spans)


#: Process-wide buffer. The OTel span processor appends to it; a run drains its own trace.
COLLECTOR = SpanCollector()


def drain_spans(*, trace_id: str | None = None) -> list[SpanRecord]:
    return COLLECTOR.drain(trace_id=trace_id)
