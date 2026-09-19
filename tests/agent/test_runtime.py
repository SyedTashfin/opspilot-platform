from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from opspilot.agent.runtime import AgentRuntime
from opspilot.agent.store import InMemoryRunStore
from opspilot.agent.types import RunLimits, RunState, StepResult
from opspilot.domain.enums import RunStatus, StepStatus
from opspilot.gateway.accounting import InMemoryLedger


class StubStep:
    def __init__(self, name: str, outcome: str = "succeed", calls: list[str] | None = None) -> None:
        self.name = name
        self.outcome = outcome
        self.calls = calls
        self.on_run: Any = None

    async def run(self, state: RunState) -> StepResult:
        if self.calls is not None:
            self.calls.append(self.name)
        if self.on_run is not None:
            self.on_run()
        if self.outcome == "raise":
            raise RuntimeError("step exploded")
        if self.outcome == "fail":
            return StepResult.failed(f"{self.name} failed on purpose")
        if self.outcome == "wait":
            return StepResult.waiting_approval(f"{self.name} needs a human")
        return StepResult.succeeded(f"{self.name} ok")


@dataclass
class RecordingTracer:
    spans: list[tuple[str, int]] = field(default_factory=list)

    def step_span(self, *, name: str, run_id: uuid.UUID, index: int, attributes: Mapping[str, Any]) -> Any:
        from contextlib import contextmanager

        @contextmanager
        def _span() -> Any:
            self.spans.append((name, index))
            yield None

        return _span()


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def build(
    steps: list[StubStep],
    *,
    limits: RunLimits | None = None,
    ledger: InMemoryLedger | None = None,
    tracer: RecordingTracer | None = None,
    clock: FakeClock | None = None,
    store: InMemoryRunStore | None = None,
) -> tuple[AgentRuntime, InMemoryRunStore]:
    resolved_store = store or InMemoryRunStore()
    if "opspilot" not in resolved_store.agents:
        resolved_store.register_agent("opspilot")
    runtime = AgentRuntime(
        store=resolved_store,
        ledger=ledger or InMemoryLedger(),
        limits=limits or RunLimits(max_steps=10, timeout_seconds=100, cost_cap_eur=0.25),
        tracer=tracer,
        clock=clock or FakeClock(),
    )
    return runtime, resolved_store


REQUEST: dict[str, Any] = {"scenario_id": "test", "service": "api", "symptom": "latency"}


async def test_successful_run_records_every_step() -> None:
    calls: list[str] = []
    steps = [StubStep(f"step-{index}", calls=calls) for index in range(3)]
    runtime, store = build(steps)

    summary = await runtime.execute(agent="opspilot", request=REQUEST, steps=steps)

    assert summary.status is RunStatus.SUCCEEDED
    assert summary.steps_executed == 3
    assert calls == ["step-0", "step-1", "step-2"]
    assert await store.status_of(summary.run_id) is RunStatus.SUCCEEDED
    records = await store.steps(summary.run_id)
    assert [record.status for record in records] == [StepStatus.SUCCEEDED] * 3
    assert all(record.duration_ms is not None for record in records)


async def test_failed_step_stops_the_run_and_later_steps_never_run() -> None:
    calls: list[str] = []
    steps = [
        StubStep("first", calls=calls),
        StubStep("second", outcome="fail", calls=calls),
        StubStep("third", calls=calls),
    ]
    runtime, store = build(steps)

    summary = await runtime.execute(agent="opspilot", request=REQUEST, steps=steps)

    assert summary.status is RunStatus.FAILED
    assert calls == ["first", "second"]
    assert "failed on purpose" in (summary.error or "")
    records = await store.steps(summary.run_id)
    assert [record.status for record in records] == [StepStatus.SUCCEEDED, StepStatus.FAILED]


async def test_raising_step_is_contained_and_recorded() -> None:
    steps = [StubStep("boom", outcome="raise")]
    runtime, store = build(steps)

    summary = await runtime.execute(agent="opspilot", request=REQUEST, steps=steps)

    assert summary.status is RunStatus.FAILED
    assert "RuntimeError" in (summary.error or "")
    records = await store.steps(summary.run_id)
    assert records[0].status is StepStatus.FAILED


async def test_waiting_for_approval_suspends_the_run() -> None:
    calls: list[str] = []
    steps = [StubStep("gate", outcome="wait", calls=calls), StubStep("after", calls=calls)]
    runtime, store = build(steps)

    summary = await runtime.execute(agent="opspilot", request=REQUEST, steps=steps)

    assert summary.status is RunStatus.WAITING_APPROVAL
    assert calls == ["gate"]
    records = await store.steps(summary.run_id)
    assert records[0].status is StepStatus.WAITING_APPROVAL


async def test_step_budget_stops_the_run() -> None:
    calls: list[str] = []
    steps = [StubStep(f"step-{index}", calls=calls) for index in range(5)]
    runtime, _ = build(steps, limits=RunLimits(max_steps=2, timeout_seconds=100, cost_cap_eur=1))

    summary = await runtime.execute(agent="opspilot", request=REQUEST, steps=steps)

    assert summary.status is RunStatus.BUDGET_EXCEEDED
    assert calls == ["step-0", "step-1"]
    assert "step budget" in (summary.error or "")


async def test_wall_clock_budget_stops_the_run() -> None:
    clock = FakeClock()
    clock_advance = clock.advance
    slow = StubStep("slow")
    slow.on_run = lambda: clock_advance(500)
    steps = [slow, StubStep("after")]

    runtime, _ = build(steps, limits=RunLimits(max_steps=10, timeout_seconds=60, cost_cap_eur=1), clock=clock)
    summary = await runtime.execute(agent="opspilot", request=REQUEST, steps=steps)

    assert summary.status is RunStatus.TIMEOUT
    assert "wall clock" in (summary.error or "")


async def test_cost_budget_is_checked_before_the_first_step() -> None:
    calls: list[str] = []
    steps = [StubStep("step-0", calls=calls)]
    ledger = InMemoryLedger(today=0.0)
    runtime, store = build(steps, ledger=ledger)
    run_id = await store.create_run(agent="opspilot", request=REQUEST, model=None)
    ledger.by_run[run_id] = 0.30

    summary = await runtime.execute(agent="opspilot", request=REQUEST, steps=steps, resume_run_id=run_id)

    assert summary.status is RunStatus.BUDGET_EXCEEDED
    assert calls == []
    assert "cost budget" in (summary.error or "")


async def test_resuming_skips_steps_already_recorded() -> None:
    calls: list[str] = []
    steps = [StubStep("step-0", calls=calls), StubStep("step-1", calls=calls)]
    runtime, store = build(steps)

    run_id = await store.create_run(agent="opspilot", request=REQUEST, model=None)
    step_id = await store.create_step(run_id=run_id, index=0, name="step-0")
    await store.finish_step(
        step_id=step_id,
        status=StepStatus.SUCCEEDED,
        summary="done earlier",
        detail={},
        duration_ms=12,
    )

    summary = await runtime.execute(agent="opspilot", request=REQUEST, steps=steps, resume_run_id=run_id)

    assert summary.status is RunStatus.SUCCEEDED
    assert calls == ["step-1"]
    records = await store.steps(run_id)
    assert len(records) == 2


async def test_tracer_receives_one_span_per_executed_step() -> None:
    tracer = RecordingTracer()
    steps = [StubStep("step-0"), StubStep("step-1")]
    runtime, _ = build(steps, tracer=tracer)

    await runtime.execute(agent="opspilot", request=REQUEST, steps=steps)

    assert tracer.spans == [("step-0", 0), ("step-1", 1)]


async def test_unknown_agent_is_refused() -> None:
    from opspilot.agent.store import UnknownAgentError

    steps = [StubStep("step-0")]
    runtime, _ = build(steps)

    with pytest.raises(UnknownAgentError):
        await runtime.execute(agent="ghost", request=REQUEST, steps=steps)


def test_run_limits_reject_unusable_values() -> None:
    with pytest.raises(ValueError):
        RunLimits(max_steps=0)
    with pytest.raises(ValueError):
        RunLimits(timeout_seconds=0)
    with pytest.raises(ValueError):
        RunLimits(cost_cap_eur=0)
