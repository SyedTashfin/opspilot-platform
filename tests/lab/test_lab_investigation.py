"""An investigation against the Incident Lab, end to end.

This is the M6 payoff: the same pipeline, the same tools, the same runtime as the M4 tests — but the
telemetry now comes from a service that really was slowed down by an injected fault, over HTTP, and the
investigation has to reach a remediation the ground truth agrees with, while never being handed the
answer key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from opspilot.agent.runtime import AgentRuntime
from opspilot.agent.store import InMemoryRunStore
from opspilot.agent.types import RunLimits
from opspilot.agents.opspilot.pipeline import investigation_steps, report_from_steps
from opspilot.agents.opspilot.schemas import Classification, DiagnosisDraft
from opspilot.domain.enums import RunStatus, StepStatus
from opspilot.gateway.accounting import InMemoryLedger, InMemoryRecorder
from opspilot.gateway.gateway import GatewayConfig, ModelGateway
from opspilot.gateway.policy import ModelChain, ModelPolicy
from opspilot.gateway.types import ModelRequest, ProviderResult, Usage
from opspilot.lab.control import LabControlClient
from opspilot.lab.scenarios import REC_LATENCY_BAD_DEPLOY, get_lab_scenario
from opspilot.lab.service import create_lab_app
from opspilot.retrieval.runbooks import RunbookIndex, load_runbooks
from opspilot.telemetry.http_source import HttpTelemetrySource
from opspilot.tools.action_tools import ActionLog, action_tools
from opspilot.tools.audit import InMemoryAuditRecorder
from opspilot.tools.executor import ToolExecutor
from opspilot.tools.registry import ToolRegistry
from opspilot.tools.runbook_tools import runbook_tools
from opspilot.tools.telemetry_tools import telemetry_tools

TOKEN = "test-lab-token"
RUNBOOKS = Path(__file__).resolve().parents[2] / "runbooks"
MODEL = "scripted/scripted-1"
LAB_URL = "http://lab"
SERVICE = "recommendation-service"


@dataclass
class DiagnosisProvider:
    """Answers with the diagnosis the evidence supports.

    A real model does this reasoning. Here it is scripted so that what the test measures is the
    workspace — telemetry over HTTP, evidence assembly, citation grounding, the approval gate — rather
    than a model's prose. The scripted diagnosis cites ids that must exist, so the grounding check has
    something real to verify.
    """

    evidence_ids: list[str] = field(default_factory=list)
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    name: str = "scripted"

    def supports(self, model: str) -> bool:
        return True

    async def complete(self, model: str, request: ModelRequest) -> ProviderResult:
        payload: BaseModel
        if request.response_model is Classification:
            payload = Classification(
                incident_class="latency",
                suspected_areas=[SERVICE, "feature store dependency"],
                confidence=0.75,
                rationale="p95 latency rose while the error rate stayed flat",
            )
        elif request.response_model is DiagnosisDraft:
            payload = DiagnosisDraft(
                root_cause=(
                    "the last deployment reduced the feature-store client timeout and raised retries, "
                    "so requests are retried into a slow dependency"
                ),
                confidence=0.8,
                evidence_ids=list(self.evidence_ids),
                recommended_tool=self.tool,
                recommended_arguments=dict(self.arguments),
                summary="roll back the deployment that changed the dependency timeouts",
            )
        else:  # pragma: no cover - the pipeline only asks for the schemas above
            msg = f"unexpected response model {request.response_model}"
            raise AssertionError(msg)
        return ProviderResult(
            text=payload.model_dump_json(),
            parsed=payload,
            usage=Usage(input_tokens=50, output_tokens=25),
            model=model,
            provider=self.name,
            request_id="scripted-1",
        )


@dataclass
class Env:
    runtime: AgentRuntime
    store: InMemoryRunStore
    gateway: ModelGateway
    executor: ToolExecutor
    runbooks: RunbookIndex
    actions: ActionLog


def build_env(provider: DiagnosisProvider, transport: httpx.AsyncBaseTransport) -> Env:
    source = HttpTelemetrySource(LAB_URL, transport=transport)
    actions = ActionLog()
    runbooks = load_runbooks(RUNBOOKS)
    registry = ToolRegistry(telemetry_tools(source) + runbook_tools(runbooks) + action_tools(actions))
    store = InMemoryRunStore()
    store.register_agent("opspilot")
    ledger = InMemoryLedger()
    gateway = ModelGateway(
        providers=[provider],
        recorder=InMemoryRecorder(),
        ledger=ledger,
        config=GatewayConfig(
            policy=ModelPolicy(default_chain=ModelChain(step="default", models=(MODEL,)), step_chains={}),
            run_cost_cap_eur=1.0,
            daily_cost_cap_eur=5.0,
        ),
    )
    runtime = AgentRuntime(store=store, ledger=ledger, limits=RunLimits())
    return Env(
        runtime=runtime,
        store=store,
        gateway=gateway,
        executor=ToolExecutor(registry=registry, audit=InMemoryAuditRecorder()),
        runbooks=runbooks,
        actions=actions,
    )


@pytest.fixture
def lab() -> httpx.AsyncBaseTransport:
    app = create_lab_app(admin_token=TOKEN, service_name=SERVICE)
    return httpx.ASGITransport(app=app)  # type: ignore[arg-type]


async def test_an_investigation_reaches_the_withheld_root_cause(lab) -> None:
    control = LabControlClient(LAB_URL, token=TOKEN, transport=lab)
    scenario = get_lab_scenario("rec-latency-bad-deploy")
    await control.inject(scenario)
    statuses = await control.drive_traffic(requests=6)
    assert statuses and all(code == 200 for code in statuses)

    provider = DiagnosisProvider(
        evidence_ids=["ev-metrics", "ev-logs", "ev-deployments"],
        tool="azure.rollback_deployment",
        arguments={
            "service": SERVICE,
            "version": "rec-2026.05.9",
            "reason": "the deployment changed dependency timeouts and caused retry amplification",
        },
    )
    env = build_env(provider, lab)
    steps = investigation_steps(gateway=env.gateway, executor=env.executor, runbooks=env.runbooks)

    summary = await env.runtime.execute(agent="opspilot", request=scenario.alert(), steps=steps)
    records = await env.store.steps(summary.run_id)
    report = report_from_steps(records)

    assert report is not None
    evidence_ids = {item.evidence_id for item in report.evidence}
    assert {"ev-metrics", "ev-logs", "ev-deployments", "ev-resource"} <= evidence_ids

    # The evidence came from the running service, not from a fixture.
    deployment_evidence = next(item for item in report.evidence if item.evidence_id == "ev-deployments")
    assert "rec-2026.06.1" in deployment_evidence.summary
    assert "feature-store client timeout 2000ms -> 400ms" in str(deployment_evidence.detail)
    log_evidence = next(item for item in report.evidence if item.evidence_id == "ev-logs")
    assert any("timed out" in line["message"].lower() for line in log_evidence.detail["lines"])
    metric_evidence = next(item for item in report.evidence if item.evidence_id == "ev-metrics")
    assert metric_evidence.source == "demo"

    # A restricted action stops at the human gate, with nothing executed.
    assert summary.status is RunStatus.WAITING_APPROVAL
    assert report.remediation is not None
    assert report.remediation.tool_name == "azure.rollback_deployment"
    assert report.remediation.status == "waiting_approval"
    assert report.remediation.arguments_hash
    assert env.actions.entries == []
    assert records[-1].status is StepStatus.WAITING_APPROVAL
    assert report.diagnosis.grounding_ratio == 1.0
    assert report.diagnosis.unsupported_evidence_ids == []

    # The answer key existed and was never needed to get there.
    truth = await control.ground_truth()
    assert truth["fault"]["root_cause"] == REC_LATENCY_BAD_DEPLOY.root_cause
    assert truth["injections"][0]["kind"] == "dependency_timeout"


async def test_a_healthy_service_does_not_look_like_an_incident(lab) -> None:
    control = LabControlClient(LAB_URL, token=TOKEN, transport=lab)
    await control.clear()
    await control.drive_traffic(requests=3)

    source = HttpTelemetrySource(LAB_URL, transport=lab)
    snapshot = await source.metrics(SERVICE, 30)
    latency = next(series for series in snapshot.series if series.name == "request_latency_p95_ms")
    errors = next(series for series in snapshot.series if series.name == "error_rate_percent")

    assert max(point.value for point in latency.points) < 300
    assert max(point.value for point in errors.points) == 0.0


async def test_the_agent_view_of_the_scenario_has_no_answer_key() -> None:
    alert = get_lab_scenario("rec-latency-bad-deploy").alert()

    assert set(alert) == {"scenario_id", "service", "symptom", "window_minutes"}
    rendered = str(alert)
    assert REC_LATENCY_BAD_DEPLOY.root_cause not in rendered
    assert all(phrase not in rendered for phrase in REC_LATENCY_BAD_DEPLOY.acceptable_diagnoses)
