"""The evaluation suite: datasets, graders, the runner and the retrieval benchmark."""

from opspilot.evals.dataset import build_dataset, runbook_dir_for, scenario_for
from opspilot.evals.graders import RunFacts, grade_case, structural_gate
from opspilot.evals.providers import ReferenceProvider
from opspilot.evals.retrieval_bench import bench
from opspilot.evals.runner import SuiteConfig, run_case, run_suite
from opspilot.evals.types import (
    CaseResult,
    EvalCase,
    GradeKind,
    GradeName,
    GradeResult,
    SuiteReport,
)

__all__ = [
    "CaseResult",
    "EvalCase",
    "GradeKind",
    "GradeName",
    "GradeResult",
    "ReferenceProvider",
    "RunFacts",
    "SuiteConfig",
    "SuiteReport",
    "bench",
    "build_dataset",
    "grade_case",
    "run_case",
    "run_suite",
    "runbook_dir_for",
    "scenario_for",
    "structural_gate",
]
