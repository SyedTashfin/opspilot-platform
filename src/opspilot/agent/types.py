"""Runtime value types.

A step is a small, named, independently recorded unit of work. It receives the run state, returns a
result, and never decides whether the run continues — that belongs to the runtime, which owns the
budgets and writes the record.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

from opspilot.domain.enums import RunStatus, StepStatus


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Deterministic ceilings. Every run has them; none of them is optional."""

    max_steps: int = 40
    timeout_seconds: int = 900
    cost_cap_eur: float = 0.25

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            msg = "max_steps must be at least 1"
            raise ValueError(msg)
        if self.timeout_seconds < 1:
            msg = "timeout_seconds must be at least 1"
            raise ValueError(msg)
        if self.cost_cap_eur <= 0:
            msg = "cost_cap_eur must be positive"
            raise ValueError(msg)


@dataclass
class RunState:
    """Shared, in-memory state for one run. Artefacts are the hand-off between steps."""

    run_id: uuid.UUID
    agent: str
    request: Mapping[str, Any]
    artefacts: dict[str, Any] = field(default_factory=dict)

    def put(self, key: str, value: Any) -> None:
        self.artefacts[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.artefacts.get(key, default)

    def require(self, key: str) -> Any:
        if key not in self.artefacts:
            msg = f"run {self.run_id} reached a step with no {key!r} in state"
            raise KeyError(msg)
        return self.artefacts[key]


@dataclass(frozen=True, slots=True)
class StepResult:
    status: StepStatus
    summary: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    @staticmethod
    def succeeded(summary: str, **detail: Any) -> StepResult:
        return StepResult(status=StepStatus.SUCCEEDED, summary=summary, detail=detail)

    @staticmethod
    def failed(summary: str, **detail: Any) -> StepResult:
        return StepResult(status=StepStatus.FAILED, summary=summary, detail=detail)

    @staticmethod
    def waiting_approval(summary: str, **detail: Any) -> StepResult:
        return StepResult(status=StepStatus.WAITING_APPROVAL, summary=summary, detail=detail)

    @staticmethod
    def skipped(summary: str, **detail: Any) -> StepResult:
        return StepResult(status=StepStatus.SKIPPED, summary=summary, detail=detail)


class AgentStep(Protocol):
    name: str

    async def run(self, state: RunState) -> StepResult: ...


class StepTracer(Protocol):
    """Minimal tracing contract.

    The OTel implementation arrives in M5; tests use a recording fake.
    """

    def step_span(
        self,
        *,
        name: str,
        run_id: uuid.UUID,
        index: int,
        attributes: Mapping[str, Any],
    ) -> AbstractContextManager[Any]: ...


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: uuid.UUID
    status: RunStatus
    steps_executed: int
    duration_ms: int
    cost_eur: float
    error: str | None = None
