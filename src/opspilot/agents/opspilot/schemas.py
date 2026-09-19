"""OpsPilot's structured contracts.

Two families, kept apart on purpose:

* **Draft types** (``Classification``, ``DiagnosisDraft``) are what a model is asked to produce.
  They
  are the schema handed to the provider.
* **Stored types** (``Diagnosis``, ``EvidenceItem``, ``InvestigationReport``) are what the platform
  records after its own deterministic checks — including which citations could not be verified.

A draft never reaches the database unexamined.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

IncidentClass = Literal["latency", "errors", "availability", "deployment", "resource", "dependency"]
EvidenceKind = Literal["metric", "log", "deployment", "resource", "runbook"]


class IncidentAlert(BaseModel):
    """The input a run starts from: what monitoring reported, nothing more."""

    scenario_id: str
    service: str
    symptom: str
    window_minutes: int = Field(default=30, ge=1, le=180)


class Classification(BaseModel):
    incident_class: IncidentClass
    suspected_areas: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class EvidenceItem(BaseModel):
    evidence_id: str
    kind: EvidenceKind
    source: str
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)
    citation_id: str | None = None
    content_hash: str | None = None


class DiagnosisDraft(BaseModel):
    """What the model returns. ``evidence_ids`` must reference the evidence it was shown."""

    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)
    recommended_tool: str | None = None
    recommended_arguments: dict[str, Any] = Field(default_factory=dict)
    summary: str


class Diagnosis(BaseModel):
    """The draft after grounding: unverifiable citations are recorded, not silently dropped."""

    root_cause: str
    confidence: float
    evidence_ids: list[str]
    unsupported_evidence_ids: list[str]
    grounding_ratio: float = Field(ge=0.0, le=1.0)
    recommended_tool: str | None
    recommended_arguments: dict[str, Any]
    summary: str


class RemediationProposal(BaseModel):
    tool_name: str | None
    arguments: dict[str, Any] = Field(default_factory=dict)
    justification: str
    requires_approval: bool
    status: Literal[
        "not_required", "proposed", "waiting_approval", "executed", "rejected", "failed"
    ]
    arguments_hash: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class InvestigationReport(BaseModel):
    run_id: uuid.UUID
    alert: IncidentAlert
    classification: Classification
    evidence: list[EvidenceItem]
    diagnosis: Diagnosis
    remediation: RemediationProposal | None = None
    model: str | None = None
    data_sources: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def ground_diagnosis(draft: DiagnosisDraft, evidence: list[EvidenceItem]) -> Diagnosis:
    """Deterministic grounding check.

    Every cited id is verified against the evidence actually shown to the model. Unverifiable ids
    are
    reported rather than quietly removed: a diagnosis that cites evidence it never saw is a finding
    about the diagnosis, and the evaluation suite (M7) scores exactly this.
    """
    known = {item.evidence_id for item in evidence}
    cited = list(dict.fromkeys(draft.evidence_ids))
    supported = [evidence_id for evidence_id in cited if evidence_id in known]
    unsupported = [evidence_id for evidence_id in cited if evidence_id not in known]
    ratio = (len(supported) / len(cited)) if cited else 0.0
    return Diagnosis(
        root_cause=draft.root_cause,
        confidence=draft.confidence,
        evidence_ids=supported,
        unsupported_evidence_ids=unsupported,
        grounding_ratio=round(ratio, 4),
        recommended_tool=draft.recommended_tool,
        recommended_arguments=dict(draft.recommended_arguments),
        summary=draft.summary,
    )
