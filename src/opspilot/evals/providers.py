"""Providers the evaluation suite runs on.

* ``ReferenceProvider`` is deterministic and answers from the case's own answer key. It exists to
  exercise the harness end to end: pipeline, evidence assembly, citation grounding, the approval gate,
  the graders. **Its semantic grades measure the harness's ceiling, not a model's ability**, and the
  report says so — a suite that gated on a reference provider's root-cause score would be measuring
  nothing but its own plumbing.
* ``FakeProvider`` (from the gateway) answers with a valid but meaningless structured object, including a
  citation to an id that does not exist. It is how the graders are shown to detect a bad answer.
* A configured provider (DeepSeek, Mistral, ...) is the real measurement: same suite, same gates, and
  the first run that can honestly be called an evaluation of a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from opspilot.agents.opspilot.schemas import Classification, DiagnosisDraft
from opspilot.gateway.types import ModelRequest, ProviderResult, Usage

REFERENCE_LABEL = "reference"


@dataclass
class ReferenceProvider:
    """Answers from the answer key, deterministically. Measures the harness, not a model."""

    evidence_ids: list[str] = field(default_factory=list)
    root_cause: str = ""
    summary: str = "remediate according to the runbook for this symptom"
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    incident_class: str = "latency"
    confidence: float = 0.82
    name: str = REFERENCE_LABEL
    calls: list[str] = field(default_factory=list)

    def supports(self, model: str) -> bool:
        return True

    async def complete(self, model: str, request: ModelRequest) -> ProviderResult:
        self.calls.append(request.step)
        payload: BaseModel
        if request.response_model is Classification:
            payload = Classification(
                incident_class=self.incident_class,
                suspected_areas=["the alerted service", "its dependencies"],
                confidence=0.7,
                rationale="classified from the alert symptom",
            )
        elif request.response_model is DiagnosisDraft:
            payload = DiagnosisDraft(
                root_cause=self.root_cause,
                confidence=self.confidence,
                evidence_ids=list(self.evidence_ids),
                recommended_tool=self.tool,
                recommended_arguments=dict(self.arguments),
                summary=self.summary,
            )
        else:  # pragma: no cover - the pipeline asks for the two schemas above
            msg = f"reference provider asked for an unexpected schema: {request.response_model}"
            raise AssertionError(msg)
        return ProviderResult(
            text=payload.model_dump_json(),
            parsed=payload,
            usage=Usage(input_tokens=120, output_tokens=40),
            model=model,
            provider=self.name,
            request_id=f"reference-{len(self.calls)}",
        )
