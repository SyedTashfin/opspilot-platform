"""The suite runner: inject a scenario, investigate it, grade what happened, report.

The lab runs in process (ASGI, no container) so the suite is deterministic and CI-runnable; the compose
service is the same application for the demo. A real provider key changes the model, not the harness —
which is the property that makes the numbers comparable between a `reference` run and a live run.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from opspilot.agent.runtime import AgentRuntime
from opspilot.agent.store import InMemoryRunStore, StepRecord
from opspilot.agent.types import RunLimits
from opspilot.agents.opspilot.pipeline import investigation_steps, report_from_steps
from opspilot.agents.opspilot.schemas import InvestigationReport
from opspilot.evals.dataset import runbook_dir_for, scenario_for
from opspilot.evals.graders import RunFacts, grade_case, structural_gate
from opspilot.evals.retrieval_bench import bench
from opspilot.evals.types import CaseResult, EvalCase, SuiteReport
from opspilot.gateway.accounting import InMemoryLedger, InMemoryRecorder
from opspilot.gateway.gateway import GatewayConfig, ModelGateway
from opspilot.gateway.policy import ModelChain, ModelPolicy
from opspilot.gateway.providers.base import ModelProvider
from opspilot.lab.control import LabControlClient
from opspilot.lab.service import DEFAULT_ADMIN_TOKEN, create_lab_app
from opspilot.observability.logging import get_logger
from opspilot.retrieval.runbooks import load_runbooks
from opspilot.telemetry.http_source import HttpTelemetrySource
from opspilot.tools.action_tools import ActionLog, action_tools
from opspilot.tools.audit import InMemoryAuditRecorder
from opspilot.tools.executor import ToolExecutor
from opspilot.tools.registry import ToolRegistry
from opspilot.tools.runbook_tools import runbook_tools
from opspilot.tools.telemetry_tools import telemetry_tools

logger = get_logger(__name__)

LAB_URL = "http://lab-inproc"
EVAL_MODEL = "eval/eval-1"


@dataclass
class SuiteConfig:
    provider_factory: Callable[[EvalCase], ModelProvider]
    provider_label: str
    model: str = EVAL_MODEL
    lab_token: str = DEFAULT_ADMIN_TOKEN
    max_duration_ms: int = 60_000
    max_cost_eur: float = 0.25
    requests_per_case: int = 2
    include_retrieval_bench: bool = True
    run_limits: RunLimits = field(default_factory=RunLimits)
    notes: tuple[str, ...] = ()


@dataclass
class CaseOutcome:
    result: CaseResult
    report: InvestigationReport | None
    records: Sequence[StepRecord]


def _gateway(provider: ModelProvider, ledger: InMemoryLedger, model: str) -> ModelGateway:
    return ModelGateway(
        providers=[provider],
        recorder=InMemoryRecorder(),
        ledger=ledger,
        config=GatewayConfig(
            policy=ModelPolicy(default_chain=ModelChain(step="default", models=(model,)), step_chains={}),
            run_cost_cap_eur=1.0,
            daily_cost_cap_eur=10.0,
        ),
    )


async def run_case(
    case: EvalCase,
    config: SuiteConfig,
    *,
    transport: httpx.AsyncBaseTransport,
    control: LabControlClient,
) -> CaseOutcome:
    """Investigate one case against a lab the caller has already prepared."""
    scenario = scenario_for(case)
    await control.inject(scenario)
    await control.drive_traffic(requests=config.requests_per_case)

    provider = config.provider_factory(case)
    source = HttpTelemetrySource(LAB_URL, transport=transport)
    actions = ActionLog()
    runbooks = load_runbooks(runbook_dir_for(case))
    registry = ToolRegistry(telemetry_tools(source) + runbook_tools(runbooks) + action_tools(actions))
    store = InMemoryRunStore()
    store.register_agent("opspilot")
    ledger = InMemoryLedger()
    gateway = _gateway(provider, ledger, config.model)
    runtime = AgentRuntime(store=store, ledger=ledger, limits=config.run_limits)
    steps = investigation_steps(
        gateway=gateway,
        executor=ToolExecutor(registry=registry, audit=InMemoryAuditRecorder()),
        runbooks=runbooks,
    )

    started = time.perf_counter()
    summary = await runtime.execute(agent="opspilot", request=case.alert, steps=steps)
    duration_ms = int((time.perf_counter() - started) * 1000)
    records = await store.steps(summary.run_id)
    report = report_from_steps(records)

    facts = RunFacts.from_step_records(
        records,
        run_status=summary.status.value,
        duration_ms=duration_ms,
        cost_eur=summary.cost_eur,
        model=config.model,
    )
    # The action log is the ground truth about what actually ran, so a proposed-but-refused action can
    # never be mistaken for an executed one.
    facts = RunFacts(
        run_status=facts.run_status,
        duration_ms=facts.duration_ms,
        cost_eur=facts.cost_eur,
        tools_used=facts.tools_used,
        tools_refused=facts.tools_refused,
        actions_executed=tuple(entry["tool"] for entry in actions.entries),
        approvals_waited=facts.approvals_waited,
        model=facts.model,
    )
    grades = grade_case(
        case,
        report,
        facts,
        max_duration_ms=config.max_duration_ms,
        max_cost_eur=config.max_cost_eur,
    )
    notes: list[str] = []
    if report is not None and report.injection_flags:
        notes.append(f"{len(report.injection_flags)} injection flag(s) recorded")
    return CaseOutcome(
        result=CaseResult(
            case_id=case.case_id,
            run_id=str(summary.run_id),
            run_status=summary.status.value,
            grades=grades,
            duration_ms=duration_ms,
            cost_eur=summary.cost_eur,
            model=config.model,
            notes=tuple(notes),
        ),
        report=report,
        records=records,
    )


async def run_suite(cases: Sequence[EvalCase], config: SuiteConfig) -> SuiteReport:
    """Run every case against one in-process lab, then the retrieval benchmark."""
    app = create_lab_app(admin_token=config.lab_token)
    transport = httpx.ASGITransport(app=app)
    control = LabControlClient(LAB_URL, token=config.lab_token, transport=transport)

    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    results: list[CaseResult] = []
    for case in cases:
        await control.clear()
        outcome = await run_case(case, config, transport=transport, control=control)
        results.append(outcome.result)
        logger.info(
            "evals.case",
            case=case.case_id,
            status=outcome.result.run_status,
            structural=outcome.result.structural_passed,
            semantic=outcome.result.semantic_score,
        )

    all_grades = [grade for result in results for grade in result.grades]
    gate = structural_gate(all_grades)
    retrieval: dict[str, Any] = {}
    if config.include_retrieval_bench:
        from opspilot.evals.dataset import RUNBOOKS

        retrieval = bench(load_runbooks(RUNBOOKS))
        logger.info(
            "evals.retrieval",
            hit_at_1=retrieval["hit_at_1_rate"],
            hit_at_k=retrieval["hit_at_k_rate"],
            misses=retrieval["misses"],
        )

    return SuiteReport(
        provider=config.provider_label,
        model=config.model,
        started_at=started_at,
        duration_ms=int((time.perf_counter() - started) * 1000),
        cases=tuple(results),
        structural_gate_passed=gate.passed,
        retrieval=retrieval,
    )
