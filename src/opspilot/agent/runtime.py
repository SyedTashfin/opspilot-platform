"""The agent runtime: budgets, persistence, resumption and failure containment.

What the runtime guarantees, and what it deliberately does not:

* **Guarantees** — every step is recorded before and after execution; every run has hard ceilings on
  steps, wall-clock time and cost; a step that raises is contained, recorded as failed and ends the
  run; a step that needs human approval suspends the run rather than pretending to continue.
* **Does not** — decide what a step means, retry a failed step, or hide a failure.
  The runtime reports;
  the pipeline decides.

Resumption is index-based: steps already recorded as succeeded or skipped are not re-executed, so a
run that suspended for approval continues from where it stopped.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from opspilot.agent.store import RunStore
from opspilot.agent.types import AgentStep, RunLimits, RunState, RunSummary, StepResult, StepTracer
from opspilot.domain.enums import RunStatus, StepStatus
from opspilot.gateway.accounting import CostLedger
from opspilot.observability.logging import get_logger
from opspilot.observability.spans import drain_spans
from opspilot.observability.tracing import current_trace_id, record_error, set_attributes, span

logger = get_logger(__name__)

RESUMABLE_STATUSES = (StepStatus.SUCCEEDED, StepStatus.SKIPPED)


@dataclass
class AgentRuntime:
    store: RunStore
    ledger: CostLedger
    limits: RunLimits
    tracer: StepTracer | None = None
    clock: Callable[[], float] = time.monotonic

    async def execute(
        self,
        *,
        agent: str,
        request: Mapping[str, Any],
        steps: Sequence[AgentStep],
        model: str | None = None,
        trigger: str = "manual",
        resume_run_id: uuid.UUID | None = None,
    ) -> RunSummary:
        pipeline = tuple(steps)
        run_id = resume_run_id or await self.store.create_run(
            agent=agent, request=request, model=model, trigger=trigger
        )
        state = RunState(run_id=run_id, agent=agent, request=request)
        started = self.clock()

        completed = {
            record.index for record in await self.store.steps(run_id) if record.status in RESUMABLE_STATUSES
        }
        if completed:
            logger.info("runtime.resuming", run_id=str(run_id), completed=len(completed))

        with span(
            "agent.run", {"agent.name": agent, "run.id": str(run_id), "run.trigger": trigger}
        ) as run_span:
            trace_id = current_trace_id()
            if trace_id is not None:
                await self.store.attach_trace(run_id=run_id, trace_id=trace_id)
            status, error, executed = await self._drive(
                pipeline=pipeline,
                state=state,
                run_id=run_id,
                agent=agent,
                completed=completed,
                started=started,
            )
            set_attributes(
                run_span,
                {
                    "run.status": status.value,
                    "run.steps_executed": executed,
                    "run.failed": status is RunStatus.FAILED,
                },
            )
            if status in (RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.BUDGET_EXCEEDED):
                # A run that ended for a reason other than success is an error span. A run suspended for
                # approval is not: waiting for a human is the system working.
                record_error(run_span, error or status.value, status.value)

        duration_ms = int((self.clock() - started) * 1000)
        cost_eur = await self.ledger.spent_in_run_eur(run_id)
        await self.store.finish_run(run_id=run_id, status=status, duration_ms=duration_ms, error=error)
        if trace_id is not None:
            # Draining after the run span has ended is what puts the run span itself in the trace.
            await self.store.record_spans(run_id=run_id, spans=drain_spans(trace_id=trace_id))
        logger.info(
            "runtime.finished",
            run_id=str(run_id),
            status=status.value,
            steps_executed=executed,
            duration_ms=duration_ms,
            cost_eur=round(cost_eur, 6),
        )
        return RunSummary(
            run_id=run_id,
            status=status,
            steps_executed=executed,
            duration_ms=duration_ms,
            cost_eur=cost_eur,
            error=error,
        )

    async def _drive(
        self,
        *,
        pipeline: tuple[AgentStep, ...],
        state: RunState,
        run_id: uuid.UUID,
        agent: str,
        completed: set[int],
        started: float,
    ) -> tuple[RunStatus, str | None, int]:
        """Run the pipeline from the first step that has not already been recorded."""
        status = RunStatus.SUCCEEDED
        error: str | None = None
        executed = 0

        for index, step in enumerate(pipeline):
            if index in completed:
                continue
            ceiling = await self._ceiling_hit(run_id, executed=executed, started=started)
            if ceiling is not None:
                status, error = ceiling
                break

            step_id = await self.store.create_step(run_id=run_id, index=index, name=step.name)
            step_started = self.clock()
            result = await self._run_step(step, state, index=index, agent=agent, run_id=run_id)
            duration_ms = int((self.clock() - step_started) * 1000)
            await self.store.finish_step(
                step_id=step_id,
                status=result.status,
                summary=result.summary,
                detail=result.detail,
                duration_ms=duration_ms,
            )
            executed += 1

            if result.status is StepStatus.FAILED:
                status, error = RunStatus.FAILED, result.summary
                break
            if result.status is StepStatus.WAITING_APPROVAL:
                status = RunStatus.WAITING_APPROVAL
                break

        return status, error, executed

    async def _run_step(
        self, step: AgentStep, state: RunState, *, index: int, agent: str, run_id: uuid.UUID
    ) -> StepResult:
        """Run one step, containing every failure and tracing it if a tracer is configured."""
        step_span_ctx = (
            self.tracer.step_span(
                name=step.name,
                run_id=run_id,
                index=index,
                attributes={"agent": agent, "step.name": step.name},
            )
            if self.tracer is not None
            else nullcontext()
        )
        try:
            with step_span_ctx:
                return await step.run(state)
        except Exception as exc:
            logger.warning(
                "runtime.step_raised",
                step=step.name,
                run_id=str(run_id),
                error=f"{type(exc).__name__}: {exc}",
            )
            return StepResult.failed(
                f"{step.name} raised {type(exc).__name__}: {exc}",
                step=step.name,
                exception=type(exc).__name__,
            )

    async def _ceiling_hit(
        self, run_id: uuid.UUID, *, executed: int, started: float
    ) -> tuple[RunStatus, str] | None:
        if executed >= self.limits.max_steps:
            return (
                RunStatus.BUDGET_EXCEEDED,
                f"step budget reached ({self.limits.max_steps} steps)",
            )
        elapsed = self.clock() - started
        if elapsed > self.limits.timeout_seconds:
            return (
                RunStatus.TIMEOUT,
                f"wall clock budget reached ({elapsed:.0f}s of {self.limits.timeout_seconds}s)",
            )
        spent = await self.ledger.spent_in_run_eur(run_id)
        if spent >= self.limits.cost_cap_eur:
            return (
                RunStatus.BUDGET_EXCEEDED,
                f"cost budget reached ({spent:.4f} of {self.limits.cost_cap_eur:.4f} EUR)",
            )
        return None
