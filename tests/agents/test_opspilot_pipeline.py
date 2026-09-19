from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from opspilot.agent.runtime import AgentRuntime
from opspilot.agent.store import InMemoryRunStore
from opspilot.agent.types import RunLimits
from opspilot.agents.opspilot.pipeline import investigation_steps, report_from_steps
from opspilot.agents.opspilot.schemas import Classification, DiagnosisDraft, IncidentAlert
from opspilot.domain.enums import RunStatus, StepStatus
from opspilot.gateway.accounting import InMemoryLedger, InMemoryRecorder
from opspilot.gateway.gateway import GatewayConfig, ModelGateway
from opspilot.gateway.policy import ModelChain, ModelPolicy
from opspilot.gateway.providers.fake import FakeProvider
from opspilot.gateway.types import ModelRequest, ProviderResult, Usage
from opspilot.retrieval.runbooks import RunbookIndex, load_runbooks
from opspilot.telemetry.scenarios import get_scenario
from opspilot.telemetry.synthetic import SyntheticTelemetrySource
from opspilot.tools.action_tools import ActionLog, action_tools
from opspilot.tools.audit import InMemoryAuditRecorder
from opspilot.tools.executor import ToolExecutor
from opspilot.tools.registry import ToolRegistry
from opspilot.tools.runbook_tools import runbook_tools
from opspilot.tools.telemetry_tools import telemetry_tools

RUNBOOK_DIR = Path(__file__).resolve().parents[2] / "runbooks"
SCENARIO = get_scenario("rec-latency-bad-deploy")
MODEL = "fake/fake-1"
EXPECTED_STEPS = [
    "classify",
    "collect_metrics",
    "collect_logs",
    "collect_deployments",
    "collect_resource_state",
    "retrieve_runbook",
    "select_runbook_chunks",
    "assemble_evidence",
    "diagnose",
    "write_report",
    "propose_remediation",
]


@dataclass
class ScriptedProvider:
    """A provider that returns a fixed structured payload per response model."""

    name: str = "scripted"
    payloads: dict[type[BaseModel], BaseModel] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def supports(self, model: str) -> bool:
        return True

    async def complete(self, model: str, request: ModelRequest) -> ProviderResult:
        self.calls.append(request.step)
        payload = self.payloads.get(request.response_model) if request.response_model else None
        return ProviderResult(
            text=payload.model_dump_json() if payload is not None else "no structured payload",
            usage=Usage(input_tokens=20, output_tokens=10),
            model=model,
            provider=self.name,
            request_id="scripted-1",
            parsed=payload,
        )


@dataclass
class Harness:
    runtime: AgentRuntime
    store: InMemoryRunStore
    gateway: ModelGateway
    executor: ToolExecutor
    runbooks: RunbookIndex
    actions: ActionLog
    audit: InMemoryAuditRecorder
    provider: Any


def build_harness(provider: Any = None) -> Harness:
    resolved = provider or FakeProvider()
    actions = ActionLog()
    audit = InMemoryAuditRecorder()
    runbooks = load_runbooks(RUNBOOK_DIR)
    registry = ToolRegistry(
        telemetry_tools(SyntheticTelemetrySource(scenario=SCENARIO))
        + runbook_tools(runbooks)
        + action_tools(actions)
    )
    executor = ToolExecutor(registry=registry, audit=audit)
    store = InMemoryRunStore()
    store.register_agent("opspilot")
    ledger = InMemoryLedger()
    gateway = ModelGateway(
        providers=[resolved],
        recorder=InMemoryRecorder(),
        ledger=ledger,
        config=GatewayConfig(
            policy=ModelPolicy(
                default_chain=ModelChain(step="default", models=(MODEL,)),
                step_chains={"classify": ModelChain(step="classify", models=(MODEL,))},
            ),
            run_cost_cap_eur=1.0,
            daily_cost_cap_eur=5.0,
        ),
    )
    runtime = AgentRuntime(store=store, ledger=ledger, limits=RunLimits())
    return Harness(
        runtime=runtime,
        store=store,
        gateway=gateway,
        executor=executor,
        runbooks=runbooks,
        actions=actions,
        audit=audit,
        provider=resolved,
    )


def alert() -> IncidentAlert:
    return IncidentAlert(
        scenario_id=SCENARIO.scenario_id,
        service=SCENARIO.service,
        symptom=SCENARIO.alert_symptom,
        window_minutes=30,
    )


async def run_pipeline(harness: Harness) -> tuple[Any, list[Any]]:
    steps = investigation_steps(gateway=harness.gateway, executor=harness.executor, runbooks=harness.runbooks)
    summary = await harness.runtime.execute(agent="opspilot", request=alert().model_dump(), steps=steps)
    return summary, await harness.store.steps(summary.run_id)


def terminating_provider(tool: str, arguments: dict[str, Any], evidence_ids: list[str]) -> Any:
    """A provider whose diagnosis recommends one specific action."""
    return ScriptedProvider(
        payloads={
            Classification: Classification(
                incident_class="latency",
                suspected_areas=["recommendation-service", "feature store dependency"],
                confidence=0.7,
                rationale="p95 latency rose sharply while error rate stayed low",
            ),
            DiagnosisDraft: DiagnosisDraft(
                root_cause="the latest deployment changed dependency timeout and retry behaviour",
                confidence=0.8,
                evidence_ids=evidence_ids,
                recommended_tool=tool,
                recommended_arguments=arguments,
                summary="roll the deployment back to the previous version",
            ),
        }
    )


async def test_investigation_records_every_step_in_order() -> None:
    harness = build_harness()

    summary, records = await run_pipeline(harness)

    assert summary.status is RunStatus.SUCCEEDED
    assert [record.name for record in records] == EXPECTED_STEPS
    assert all(record.status is StepStatus.SUCCEEDED for record in records)
    assert all(record.duration_ms is not None for record in records)
    assert summary.steps_executed == len(EXPECTED_STEPS)


async def test_report_is_assembled_with_citable_evidence() -> None:
    harness = build_harness()

    _, records = await run_pipeline(harness)
    report = report_from_steps(records)

    assert report is not None
    assert report.alert.scenario_id == SCENARIO.scenario_id
    ids = {item.evidence_id for item in report.evidence}
    assert {"ev-metrics", "ev-logs", "ev-deployments", "ev-resource"} <= ids
    assert any(item.kind == "runbook" for item in report.evidence)
    assert report.classification.incident_class
    assert report.data_sources == ["demo", "runbook"]


async def test_step_trace_never_contains_the_ground_truth() -> None:
    harness = build_harness()

    _, records = await run_pipeline(harness)
    serialized = json.dumps(
        [{"name": record.name, "summary": record.summary, "detail": record.detail} for record in records]
    )

    assert SCENARIO.root_cause not in serialized
    assert SCENARIO.injected_fault not in serialized
    assert "Chain-of-thought" not in serialized


async def test_invented_citations_are_flagged_not_hidden() -> None:
    """The deterministic fake cites an evidence id that was never collected."""
    harness = build_harness()

    _, records = await run_pipeline(harness)
    report = report_from_steps(records)

    assert report is not None
    assert report.diagnosis.evidence_ids == []
    assert report.diagnosis.unsupported_evidence_ids == ["fake evidence_ids"]
    assert report.diagnosis.grounding_ratio == 0.0
    assert any("do not exist" in note for note in report.notes)


async def test_restricted_remediation_suspends_the_run_for_human_approval() -> None:
    provider = terminating_provider(
        "azure.restart_service",
        {"service": SCENARIO.service, "reason": "restart after a bad deployment"},
        ["ev-logs", "ev-deployments"],
    )
    harness = build_harness(provider)

    summary, records = await run_pipeline(harness)
    report = report_from_steps(records)

    assert summary.status is RunStatus.WAITING_APPROVAL
    assert records[-1].name == "propose_remediation"
    assert records[-1].status is StepStatus.WAITING_APPROVAL
    assert report is not None
    assert report.remediation is not None
    assert report.remediation.status == "waiting_approval"
    assert report.remediation.requires_approval is True
    assert report.remediation.arguments_hash
    assert report.diagnosis.evidence_ids == ["ev-logs", "ev-deployments"]
    assert report.diagnosis.grounding_ratio == 1.0

    # Nothing ran, and the refusal is on the record.
    assert harness.actions.entries == []
    assert "azure.restart_service" in harness.audit.subjects()

    # The investigation report a human needs exists before the gate they must clear.
    names = [record.name for record in records]
    assert names[-2:] == ["write_report", "propose_remediation"]
    assert report.evidence
    assert report.classification.incident_class == "latency"


async def test_a_run_that_suspends_records_the_proposal_as_data() -> None:
    provider = terminating_provider(
        "azure.rollback_deployment",
        {"service": SCENARIO.service, "version": "rec-2026.05.9", "reason": "bad deployment"},
        ["ev-deployments"],
    )
    harness = build_harness(provider)

    _, records = await run_pipeline(harness)
    gate = next(record for record in records if record.name == "propose_remediation")

    assert gate.detail is not None
    assert gate.detail["remediation"]["status"] == "waiting_approval"
    assert gate.detail["arguments"] == {
        "service": SCENARIO.service,
        "version": "rec-2026.05.9",
        "reason": "bad deployment",
    }
    assert gate.detail["arguments_hash"]


async def test_a_tool_outside_the_allowlist_is_refused_before_it_is_called() -> None:
    provider = terminating_provider("shell.exec", {"command": "rm -rf /"}, ["ev-logs", "ev-deployments"])
    harness = build_harness(provider)

    summary, records = await run_pipeline(harness)

    assert summary.status is RunStatus.FAILED
    assert records[-1].name == "propose_remediation"
    assert records[-1].status is StepStatus.FAILED
    assert "outside its allowed tool set" in (records[-1].summary or "")
    assert "shell.exec" not in harness.audit.subjects()


async def test_the_pipeline_is_deterministic_across_runs() -> None:
    first = await run_pipeline(build_harness())
    second = await run_pipeline(build_harness())

    assert [record.name for record in first[1]] == [record.name for record in second[1]]
    assert first[0].status is second[0].status

    first_report = report_from_steps(first[1])
    second_report = report_from_steps(second[1])
    assert first_report is not None and second_report is not None
    assert [item.evidence_id for item in first_report.evidence] == [
        item.evidence_id for item in second_report.evidence
    ]


@pytest.mark.parametrize("step_name", ["classify", "diagnose"])
async def test_model_steps_record_which_model_answered(step_name: str) -> None:
    harness = build_harness()

    _, records = await run_pipeline(harness)
    record = next(row for row in records if row.name == step_name)

    assert record.detail is not None
    assert record.detail["model"] == MODEL
    assert "cost_known" in record.detail
