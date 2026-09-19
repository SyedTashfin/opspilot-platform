"""Evaluation vocabulary.

An evaluation run answers two different kinds of question, and this module keeps them apart:

* **Structural grades** are deterministic and gated in CI: did the run enforce its budgets, did every
  citation refer to evidence that exists, did a restricted action execute without approval, did the
  agent follow an instruction that arrived inside retrieved data. These do not need a model to be
  meaningful, so they are gates.
* **Semantic grades** need a model to answer at all: is the root cause right, is the evidence relevant,
  is the summary honest. Until a provider key exists these are *measured and reported*, never gated,
  because gating on a number produced by a placeholder model would be theatre.

Every grade carries its method and its basis, for the same reason every metric does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class GradeKind(StrEnum):
    STRUCTURAL = "structural"
    SEMANTIC = "semantic"


class GradeName(StrEnum):
    RUN_COMPLETED = "run_completed"
    BUDGETS_RESPECTED = "budgets_respected"
    CITATIONS_GROUNDED = "citations_grounded"
    NO_UNAPPROVED_ACTION = "no_unapproved_action"
    EXPECTED_TOOLS_USED = "expected_tools_used"
    FORBIDDEN_ACTIONS_AVOIDED = "forbidden_actions_avoided"
    NO_INJECTION_COMPLIANCE = "no_injection_compliance"
    ROOT_CAUSE_MATCH = "root_cause_match"


STRUCTURAL_GRADES = (
    GradeName.RUN_COMPLETED,
    GradeName.BUDGETS_RESPECTED,
    GradeName.CITATIONS_GROUNDED,
    GradeName.NO_UNAPPROVED_ACTION,
    GradeName.FORBIDDEN_ACTIONS_AVOIDED,
    GradeName.NO_INJECTION_COMPLIANCE,
)
SEMANTIC_GRADES = (GradeName.EXPECTED_TOOLS_USED, GradeName.ROOT_CAUSE_MATCH)


@dataclass(frozen=True, slots=True)
class GradeResult:
    name: GradeName
    kind: GradeKind
    passed: bool
    score: float
    method: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One scenario, one alert, and the facts needed to grade the investigation of it."""

    case_id: str
    alert: dict[str, Any]
    acceptable_diagnoses: tuple[str, ...]
    expected_tools: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    required_terms: tuple[tuple[str, ...], ...] = ()
    poison_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    run_id: str
    run_status: str
    grades: tuple[GradeResult, ...]
    duration_ms: int
    cost_eur: float
    model: str | None = None
    notes: tuple[str, ...] = ()

    def grade(self, name: GradeName) -> GradeResult | None:
        for result in self.grades:
            if result.name is name:
                return result
        return None

    @property
    def structural_passed(self) -> bool:
        return all(result.passed for result in self.grades if result.kind is GradeKind.STRUCTURAL)

    @property
    def semantic_score(self) -> float:
        graded = [result for result in self.grades if result.kind is GradeKind.SEMANTIC]
        if not graded:
            return 0.0
        return round(sum(result.score for result in graded) / len(graded), 4)


@dataclass(frozen=True, slots=True)
class SuiteReport:
    provider: str
    model: str | None
    started_at: str
    duration_ms: int
    cases: tuple[CaseResult, ...]
    structural_gate_passed: bool
    retrieval: dict[str, Any] = field(default_factory=dict)

    @property
    def passed_cases(self) -> int:
        return sum(1 for case in self.cases if case.structural_passed)

    def summary(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "cases": len(self.cases),
            "structural_gate_passed": self.structural_gate_passed,
            "cases_meeting_structural_grades": self.passed_cases,
            "mean_semantic_score": round(sum(case.semantic_score for case in self.cases) / len(self.cases), 4)
            if self.cases
            else 0.0,
            "total_cost_eur": round(sum(case.cost_eur for case in self.cases), 6),
            "duration_ms": self.duration_ms,
            "retrieval": self.retrieval,
            "failed_cases": [case.case_id for case in self.cases if not case.structural_passed],
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "started_at": self.started_at,
            "cases": [
                {
                    "case_id": case.case_id,
                    "run_id": case.run_id,
                    "run_status": case.run_status,
                    "duration_ms": case.duration_ms,
                    "cost_eur": case.cost_eur,
                    "model": case.model,
                    "notes": list(case.notes),
                    "grades": [
                        {
                            "name": grade.name.value,
                            "kind": grade.kind.value,
                            "passed": grade.passed,
                            "score": grade.score,
                            "method": grade.method,
                            "detail": grade.detail,
                        }
                        for grade in case.grades
                    ],
                }
                for case in self.cases
            ],
        }
