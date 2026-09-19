"""The OpsPilot investigation pipeline.

A fixed sequence of named steps, each recorded independently:

    classify -> metrics -> logs -> deployments -> resource state -> runbook -> evidence
    -> diagnose -> write report -> propose remediation

Why fixed rather than model-chosen: the control flow of an incident investigation is known,
repeatable and auditable. Letting a model choose its next tool would add variance to the thing the
evaluation suite must measure, and would make "why did it do that?" a matter of reconstruction
rather than record.

Every step records actions, observations and decisions. None records private reasoning.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from opspilot.agent.store import StepRecord
from opspilot.agent.types import AgentStep, RunState, StepResult
from opspilot.agents.opspilot.guardrails import detect_injections, flag_texts
from opspilot.agents.opspilot.prompts import classification_messages, diagnosis_messages
from opspilot.agents.opspilot.schemas import (
    Classification,
    Diagnosis,
    DiagnosisDraft,
    EvidenceItem,
    IncidentAlert,
    InvestigationReport,
    RemediationProposal,
    ground_diagnosis,
)
from opspilot.domain.enums import CallStatus
from opspilot.gateway.gateway import ModelGateway
from opspilot.gateway.types import ModelRequest
from opspilot.retrieval.runbooks import RunbookChunk, RunbookIndex
from opspilot.tools.executor import ToolExecutor
from opspilot.tools.runbook_tools import RunbookSearchResult
from opspilot.tools.telemetry_tools import (
    DeploymentsResult,
    LogQueryResult,
    MetricsResult,
    ResourceStateResult,
)
from opspilot.tools.types import ToolContext, ToolOutcome


def tool_context(state: RunState) -> ToolContext:
    return ToolContext(agent=state.agent, run_id=state.run_id)


@dataclass
class ClassifyStep:
    gateway: ModelGateway
    name: str = "classify"

    async def run(self, state: RunState) -> StepResult:
        alert = IncidentAlert(**state.request)
        state.put("alert", alert)
        response = await self.gateway.complete(
            ModelRequest(
                step="classify",
                messages=classification_messages(alert),
                response_model=Classification,
            ),
            state.run_id,
        )
        classification = response.parsed
        if not isinstance(classification, Classification):
            msg = "classify step received no structured Classification"
            raise TypeError(msg)
        state.put("classification", classification)
        return StepResult.succeeded(
            f"classified as {classification.incident_class} at confidence {classification.confidence:.2f}",
            incident_class=classification.incident_class,
            confidence=classification.confidence,
            model=response.model,
            cost_eur=response.cost_eur,
            cost_known=response.cost_known,
        )


@dataclass
class ToolStep:
    """One read-only tool call, recorded with its latency and typed output."""

    executor: ToolExecutor
    name: str
    tool: str
    output_model: type[BaseModel]
    state_key: str
    arguments: Callable[[RunState], dict[str, Any]]
    summarise: Callable[[Any], str]

    async def run(self, state: RunState) -> StepResult:
        arguments = self.arguments(state)
        outcome = await self.executor.execute(self.tool, arguments, tool_context(state))
        if outcome.status is not CallStatus.OK:
            return StepResult.failed(
                f"{self.tool} could not run: {outcome.status.value} — {outcome.error}",
                tool=self.tool,
                status=outcome.status.value,
            )
        output = outcome.output
        if not isinstance(output, self.output_model):
            return StepResult.failed(
                f"{self.tool} returned {type(output).__name__}, expected {self.output_model.__name__}",
                tool=self.tool,
            )
        state.put(self.state_key, output)
        return StepResult.succeeded(
            self.summarise(output),
            tool=self.tool,
            latency_ms=outcome.latency_ms,
            arguments=arguments,
        )


@dataclass
class EvidenceStep:
    name: str = "assemble_evidence"

    async def run(self, state: RunState) -> StepResult:
        items = assemble_evidence(state)
        state.put("evidence", items)
        # Retrieved text is untrusted. Scan it once, deterministically, and record what was found: the
        # flags go into the report so a reviewer can see the agent was exposed to it (M7/M8).
        flags = detect_injections({item.evidence_id: f"{item.summary} {item.detail}" for item in items})
        state.put("injection_flags", flags)
        by_kind: dict[str, int] = {}
        for item in items:
            by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
        return StepResult.succeeded(
            f"{len(items)} evidence items assembled",
            evidence_ids=[item.evidence_id for item in items],
            by_kind=by_kind,
            injection_flags=flag_texts(flags),
        )


@dataclass
class DiagnoseStep:
    gateway: ModelGateway
    name: str = "diagnose"

    async def run(self, state: RunState) -> StepResult:
        alert: IncidentAlert = state.require("alert")
        classification: Classification = state.require("classification")
        evidence: list[EvidenceItem] = state.require("evidence")
        chunks: list[RunbookChunk] = state.get("runbook_chunks", [])

        response = await self.gateway.complete(
            ModelRequest(
                step="diagnose",
                messages=diagnosis_messages(alert, classification, evidence, chunks),
                response_model=DiagnosisDraft,
            ),
            state.run_id,
        )
        draft = response.parsed
        if not isinstance(draft, DiagnosisDraft):
            msg = "diagnose step received no structured DiagnosisDraft"
            raise TypeError(msg)

        diagnosis = ground_diagnosis(draft, evidence)
        state.put("diagnosis", diagnosis)
        return StepResult.succeeded(
            f"root cause proposed at confidence {diagnosis.confidence:.2f} with grounding "
            f"{diagnosis.grounding_ratio:.2f}",
            root_cause=diagnosis.root_cause,
            confidence=diagnosis.confidence,
            evidence_ids=diagnosis.evidence_ids,
            unsupported_evidence_ids=diagnosis.unsupported_evidence_ids,
            grounding_ratio=diagnosis.grounding_ratio,
            recommended_tool=diagnosis.recommended_tool,
            model=response.model,
            cost_eur=response.cost_eur,
            cost_known=response.cost_known,
        )


@dataclass
class RemediateStep:
    """Propose and, only with approval, perform a restricted action.

    The step calls the restricted tool *without* an approval on purpose: the executor refuses,
    records
    the refusal, and the step suspends the run with ``waiting_approval``. That is the human gate
    operating, not an error path.
    """

    executor: ToolExecutor
    allowed_tools: tuple[str, ...]
    name: str = "propose_remediation"

    async def run(self, state: RunState) -> StepResult:
        diagnosis: Diagnosis = state.require("diagnosis")
        tool_name = diagnosis.recommended_tool

        if tool_name is None:
            proposal = RemediationProposal(
                tool_name=None,
                justification=diagnosis.summary,
                requires_approval=False,
                status="not_required",
            )
            state.put("remediation", proposal)
            return StepResult.succeeded(
                "no remediation proposed",
                status=proposal.status,
                proposed_tool=None,
                remediation=proposal.model_dump(mode="json"),
            )

        if tool_name not in self.allowed_tools:
            # A proposal the agent may not execute is a *finding about the proposal*, not a failure of
            # the investigation: the report is already written and the diagnosis stands. Recorded as a
            # refusal so the trace says what was proposed and why it was not run.
            refusal = RemediationProposal(
                tool_name=tool_name,
                arguments=dict(diagnosis.recommended_arguments),
                justification=diagnosis.summary,
                requires_approval=False,
                status="rejected",
                detail={"reason": f"{tool_name!r} is outside this agent's allowed tool set"},
            )
            state.put("remediation", refusal)
            return StepResult.succeeded(
                f"remediation refused: {tool_name!r} is outside this agent's allowed tool set",
                proposed_tool=tool_name,
                status=refusal.status,
                remediation=refusal.model_dump(mode="json"),
            )

        outcome: ToolOutcome = await self.executor.execute(
            tool_name, diagnosis.recommended_arguments, tool_context(state)
        )
        remediation_status: Literal["executed", "waiting_approval", "rejected", "failed"] = "failed"
        if outcome.status is CallStatus.OK:
            remediation_status = "executed"
        elif outcome.status is CallStatus.WAITING_APPROVAL:
            remediation_status = "waiting_approval"
        elif outcome.status is CallStatus.REJECTED:
            remediation_status = "rejected"
        proposal = RemediationProposal(
            tool_name=tool_name,
            arguments=dict(diagnosis.recommended_arguments),
            justification=diagnosis.summary,
            requires_approval=outcome.status is CallStatus.WAITING_APPROVAL,
            status=remediation_status,
            arguments_hash=outcome.arguments_hash,
            detail={"executor_status": outcome.status.value, "error": outcome.error},
        )
        state.put("remediation", proposal)
        payload = proposal.model_dump(mode="json")

        if outcome.status is CallStatus.OK:
            return StepResult.succeeded(
                f"{tool_name} executed after approval",
                proposed_tool=tool_name,
                status=proposal.status,
                remediation=payload,
            )
        if outcome.status is CallStatus.WAITING_APPROVAL:
            return StepResult.waiting_approval(
                f"{tool_name} requires human approval for these exact arguments",
                proposed_tool=tool_name,
                arguments=dict(diagnosis.recommended_arguments),
                arguments_hash=outcome.arguments_hash,
                status=proposal.status,
                remediation=payload,
            )
        # The executor refused for an operational reason (arguments that do not satisfy the tool's
        # schema, a timeout, an admin tool). The investigation is complete either way; the refusal is
        # recorded rather than allowed to fail the run, because a run that ends "failed" because the
        # model proposed an action it may not take would hide a good investigation behind a bad action.
        return StepResult.succeeded(
            f"{tool_name} was refused by the executor: {outcome.error}",
            proposed_tool=tool_name,
            status=proposal.status,
            remediation=payload,
        )


@dataclass
class ReportStep:
    name: str = "write_report"

    async def run(self, state: RunState) -> StepResult:
        report = build_report(state)
        state.put("report", report)
        return StepResult.succeeded(
            "investigation report assembled",
            report=report.model_dump(mode="json"),
            diagnosis_present=True,
            remediation_status=report.remediation.status if report.remediation else None,
        )


def assemble_evidence(state: RunState) -> list[EvidenceItem]:
    """Turn tool outputs into addressable evidence items. Deterministic, no model involved."""
    items: list[EvidenceItem] = []

    metrics: MetricsResult | None = state.get("metrics")
    if metrics is not None:
        ranked = sorted(metrics.series.items(), key=lambda row: row[1].max, reverse=True)
        headline = ", ".join(f"{name} p95={summary.p95}{summary.unit}" for name, summary in ranked[:3])
        items.append(
            EvidenceItem(
                evidence_id="ev-metrics",
                kind="metric",
                source=metrics.source,
                summary=f"top series by magnitude: {headline}",
                detail={name: summary.model_dump() for name, summary in metrics.series.items()},
            )
        )

    logs: LogQueryResult | None = state.get("logs")
    if logs is not None:
        notable = [line for line in logs.lines if line.level in {"WARN", "ERROR"}]
        chosen = notable[:3] or logs.lines[:3]
        items.append(
            EvidenceItem(
                evidence_id="ev-logs",
                kind="log",
                source=logs.source,
                summary=" | ".join(f"{line.level}: {line.message[:160]}" for line in chosen)
                or "no matching log lines",
                detail={
                    "count": len(logs.lines),
                    "notable_count": len(notable),
                    "lines": [
                        {"at": line.at.isoformat(), "level": line.level, "message": line.message}
                        for line in chosen
                    ],
                },
            )
        )

    deployments: DeploymentsResult | None = state.get("deployments")
    if deployments is not None and deployments.deployments:
        latest = deployments.deployments[0]
        items.append(
            EvidenceItem(
                evidence_id="ev-deployments",
                kind="deployment",
                source=deployments.source,
                summary=(
                    f"most recent deployment {latest.version} at {latest.deployed_at.isoformat()} "
                    f"changed: {'; '.join(latest.changes) or 'no change summary'}"
                ),
                detail={
                    "deployments": [
                        {
                            "version": deployment.version,
                            "deployed_at": deployment.deployed_at.isoformat(),
                            "author": deployment.author,
                            "changes": deployment.changes,
                        }
                        for deployment in deployments.deployments
                    ]
                },
            )
        )

    resource: ResourceStateResult | None = state.get("resource")
    if resource is not None:
        items.append(
            EvidenceItem(
                evidence_id="ev-resource",
                kind="resource",
                source=resource.source,
                summary=(
                    f"replicas {resource.replicas}/{resource.desired_replicas}, "
                    f"restarts last hour {resource.restarts_last_hour}, "
                    f"limits cpu={resource.cpu_limit} memory={resource.memory_limit}"
                ),
                detail=resource.model_dump(),
            )
        )

    chunks: list[RunbookChunk] = state.get("runbook_chunks", [])
    for chunk in chunks:
        items.append(
            EvidenceItem(
                evidence_id=f"rb-{chunk.citation_id}",
                kind="runbook",
                source="runbook",
                summary=f"{chunk.document} :: {chunk.heading}",
                detail={"excerpt": chunk.text.strip()[:800]},
                citation_id=chunk.citation_id,
                content_hash=chunk.content_hash,
            )
        )
    return items


def build_report(state: RunState) -> InvestigationReport:
    alert: IncidentAlert = state.require("alert")
    classification: Classification = state.require("classification")
    evidence: list[EvidenceItem] = state.require("evidence")
    diagnosis: Diagnosis = state.require("diagnosis")
    remediation: RemediationProposal | None = state.get("remediation")
    sources = sorted({item.source for item in evidence})
    flags = flag_texts(state.get("injection_flags", []))
    notes = list(flags)
    if diagnosis.unsupported_evidence_ids:
        notes.append(
            "the model cited evidence ids that do not exist: " + ", ".join(diagnosis.unsupported_evidence_ids)
        )
    if diagnosis.grounding_ratio < 1.0 and diagnosis.evidence_ids:
        notes.append(f"grounding ratio {diagnosis.grounding_ratio:.2f}")
    return InvestigationReport(
        run_id=state.run_id,
        alert=alert,
        classification=classification,
        evidence=evidence,
        diagnosis=diagnosis,
        remediation=remediation,
        model=state.get("model"),
        data_sources=sources,
        notes=notes,
        injection_flags=flags,
    )


def report_from_steps(records: Sequence[StepRecord]) -> InvestigationReport | None:
    """Read the report back from a run's recorded steps.

    The report is written when the investigation concludes — before any action is proposed — because
    that is the artefact a human reviews when deciding whether to approve one. The proposed or
    executed
    action is therefore merged in here, at read time, from whichever step recorded it.
    """
    report: InvestigationReport | None = None
    remediation: RemediationProposal | None = None
    for record in reversed(list(records)):
        detail = record.detail or {}
        if remediation is None and "remediation" in detail:
            remediation = RemediationProposal.model_validate(detail["remediation"])
        if report is None and "report" in detail:
            report = InvestigationReport.model_validate(detail["report"])
    if report is None:
        return None
    if remediation is not None:
        return report.model_copy(update={"remediation": remediation})
    return report


def investigation_steps(
    *,
    gateway: ModelGateway,
    executor: ToolExecutor,
    runbooks: RunbookIndex,
) -> list[AgentStep]:
    """The OpsPilot pipeline, wired to the tools it is allowed to use."""

    def service(state: RunState) -> str:
        return str(state.request["service"])

    def window(state: RunState) -> int:
        return int(state.request.get("window_minutes", 30))

    def runbook_query(state: RunState) -> dict[str, Any]:
        classification: Classification | None = state.get("classification")
        alert: IncidentAlert = state.require("alert")
        terms = [alert.symptom, *(classification.suspected_areas if classification else [])]
        return {"query": " ".join(terms)[:300], "limit": 3}

    return [
        ClassifyStep(gateway=gateway),
        ToolStep(
            executor=executor,
            name="collect_metrics",
            tool="azure.get_metrics",
            output_model=MetricsResult,
            state_key="metrics",
            arguments=lambda state: {"service": service(state), "window_minutes": window(state)},
            summarise=lambda output: (
                f"{len(output.series)} metric series read from {output.source} telemetry"
            ),
        ),
        ToolStep(
            executor=executor,
            name="collect_logs",
            tool="azure.query_logs",
            output_model=LogQueryResult,
            state_key="logs",
            arguments=lambda state: {
                "service": service(state),
                "window_minutes": window(state),
                "limit": 100,
            },
            summarise=lambda output: f"{len(output.lines)} log lines read from {output.source}",
        ),
        ToolStep(
            executor=executor,
            name="collect_deployments",
            tool="github.get_recent_deployments",
            output_model=DeploymentsResult,
            state_key="deployments",
            arguments=lambda state: {"service": service(state), "limit": 5},
            summarise=lambda output: f"{len(output.deployments)} deployments listed",
        ),
        ToolStep(
            executor=executor,
            name="collect_resource_state",
            tool="azure.get_resource_state",
            output_model=ResourceStateResult,
            state_key="resource",
            arguments=lambda state: {"service": service(state)},
            summarise=lambda output: (
                f"replicas {output.replicas}/{output.desired_replicas}, "
                f"{output.restarts_last_hour} restarts in the last hour"
            ),
        ),
        ToolStep(
            executor=executor,
            name="retrieve_runbook",
            tool="docs.search_runbook",
            output_model=RunbookSearchResult,
            state_key="runbook_search",
            arguments=runbook_query,
            summarise=lambda output: (
                f"{len(output.chunks)} runbook chunks retrieved from {output.indexed_chunks} indexed"
            ),
        ),
        _RunbookSelectionStep(runbooks=runbooks),
        EvidenceStep(),
        DiagnoseStep(gateway=gateway),
        # The report is written before any action is proposed: it is what the human reviews at the
        # approval gate. The proposed action is merged into it on read.
        ReportStep(),
        RemediateStep(
            executor=executor,
            allowed_tools=(
                "azure.restart_service",
                "azure.rollback_deployment",
            ),
        ),
    ]


@dataclass
class _RunbookSelectionStep:
    """Keeps the retrieved chunks as RunbookChunk objects for prompts and evidence assembly."""

    runbooks: RunbookIndex
    name: str = "select_runbook_chunks"

    async def run(self, state: RunState) -> StepResult:
        search: RunbookSearchResult = state.require("runbook_search")
        mapping = {chunk.citation_id: chunk for chunk in self.runbooks.chunks}
        chunks = [mapping[result.citation_id] for result in search.chunks if result.citation_id in mapping]
        state.put("runbook_chunks", chunks)
        return StepResult.succeeded(
            f"{len(chunks)} runbook chunks selected for the diagnosis prompt",
            citation_ids=[chunk.citation_id for chunk in chunks],
        )
