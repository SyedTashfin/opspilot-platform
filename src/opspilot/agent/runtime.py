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
            record.index
            for record in await self.store.steps(run_id)
            if record.status in RESUMABLE_STATUSES
        }
        if completed:
            logger.info("runtime.resuming", run_id=str(run_id), completed=len(completed))

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

        duration_ms = int((self.clock() - started) * 1000)
        cost_eur = await self.ledger.spent_in_run_eur(run_id)
        await self.store.finish_run(
            run_id=run_id, status=status, duration_ms=duration_ms, error=error
        )
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

    async def _run_step(
        self, step: AgentStep, state: RunState, *, index: int, agent: str, run_id: uuid.UUID
    ) -> StepResult:
        """Run one step, containing every failure and tracing it if a tracer is configured."""
        span = (
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
            with span:
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
