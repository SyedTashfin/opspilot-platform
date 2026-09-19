"""The suite end to end, on the deterministic providers.

What these tests establish:

* the harness (pipeline, evidence, grounding, approval gate, graders) works — the structural gate passes
  on a reference provider, which is what CI gates on;
* a bad answer is caught — the gateway's fake provider cites an id that does not exist and the citation
  grade fails;
* the adversarial case is exercised — the poisoned runbook is retrieved, flagged, and nothing executes;
* the retrieval benchmark produces a number, which is the evidence ADR-017 said would decide the question.
"""

from __future__ import annotations

import pytest

from opspilot.evals.dataset import build_dataset, poisoned_case, runbook_dir_for, scenario_for
from opspilot.evals.providers import ReferenceProvider
from opspilot.evals.retrieval_bench import PARAPHRASE_QUERIES, bench
from opspilot.evals.runner import SuiteConfig, run_suite
from opspilot.evals.types import GradeKind, GradeName
from opspilot.gateway.providers.fake import FAKE_MODEL, FakeProvider
from opspilot.retrieval.runbooks import load_runbooks


def reference_factory(case) -> ReferenceProvider:
    return ReferenceProvider(
        evidence_ids=["ev-metrics", "ev-logs"],
        root_cause=" ; ".join(group[0] for group in case.required_terms),
        tool="azure.rollback_deployment" if case.case_id == "rec-latency-bad-deploy" else None,
        arguments={
            "service": str(case.alert["service"]),
            "version": "rec-2026.05.9",
            "reason": "the deployment changed dependency timeouts",
        }
        if case.case_id == "rec-latency-bad-deploy"
        else {},
    )


async def test_the_suite_passes_its_structural_gate_on_a_reference_provider() -> None:
    config = SuiteConfig(
        provider_factory=reference_factory,
        provider_label="reference",
        include_retrieval_bench=True,
    )

    report = await run_suite(build_dataset(), config)
    summary = report.summary()

    assert report.structural_gate_passed, summary["failed_cases"]
    assert summary["cases"] == 4
    assert summary["cases_meeting_structural_grades"] == 4
    # Semantic grades are reported, and on a reference provider they are the harness's ceiling.
    assert summary["mean_semantic_score"] == 1.0
    assert summary["total_cost_eur"] == 0.0


async def test_every_case_produces_a_report_and_a_run_record() -> None:
    config = SuiteConfig(
        provider_factory=reference_factory, provider_label="reference", include_retrieval_bench=False
    )

    report = await run_suite(build_dataset(), config)

    for case in report.cases:
        assert case.run_id
        assert case.grade(GradeName.RUN_COMPLETED) is not None
        assert case.grade(GradeName.RUN_COMPLETED).passed  # type: ignore[union-attr]
        assert case.duration_ms > 0
        assert case.model


async def test_the_restricted_action_stops_at_the_approval_gate_in_the_suite() -> None:
    config = SuiteConfig(
        provider_factory=reference_factory, provider_label="reference", include_retrieval_bench=False
    )

    report = await run_suite(build_dataset(), config)
    rollback = next(case for case in report.cases if case.case_id == "rec-latency-bad-deploy")

    assert rollback.run_status == "waiting_approval"
    assert rollback.grade(GradeName.NO_UNAPPROVED_ACTION).passed  # type: ignore[union-attr]
    assert rollback.structural_passed


async def test_the_adversarial_case_is_retrieved_flagged_and_not_acted_on() -> None:
    """The poisoned runbook must reach the agent, be flagged, and change nothing."""
    config = SuiteConfig(
        provider_factory=reference_factory, provider_label="reference", include_retrieval_bench=False
    )

    report = await run_suite([poisoned_case()], config)
    case = report.cases[0]

    assert case.structural_passed
    assert any("injection" in note for note in case.notes)
    assert case.grade(GradeName.NO_INJECTION_COMPLIANCE).passed  # type: ignore[union-attr]
    assert case.grade(GradeName.NO_UNAPPROVED_ACTION).passed  # type: ignore[union-attr]


async def test_a_bad_answer_is_detected_by_the_citation_grade() -> None:
    """The gateway's fake provider invents an evidence id; the suite must fail that case."""
    config = SuiteConfig(
        provider_factory=lambda case: FakeProvider(),
        provider_label="fake",
        model=FAKE_MODEL,
        include_retrieval_bench=False,
    )

    report = await run_suite(build_dataset()[:1], config)
    case = report.cases[0]

    assert not report.structural_gate_passed
    assert not case.structural_passed


async def test_the_adversarial_case_uses_its_own_runbook_index() -> None:
    default_dir = runbook_dir_for(next(c for c in build_dataset() if c.case_id == "rec-error-spike"))
    poison_dir = runbook_dir_for(poisoned_case())

    assert default_dir != poison_dir
    assert {path.name for path in poison_dir.glob("*.md")} == {"connection-pool.md"}
    assert (poison_dir / "connection-pool.md").read_text().lower().count("ignore previous instructions") == 1


async def test_the_case_alert_and_the_injected_fault_agree_on_the_service() -> None:
    for case in build_dataset():
        scenario = scenario_for(case)
        assert scenario.service == case.alert["service"]
        assert scenario.alert() == case.alert
        assert scenario.fault.active


def test_the_shipped_runbooks_do_not_trip_the_injection_detector() -> None:
    """A detector that fires on normal runbooks would make the structural gate meaningless."""
    from pathlib import Path

    from opspilot.agents.opspilot.guardrails import detect_injections

    chunks = load_runbooks(Path(__file__).resolve().parents[2] / "runbooks").chunks
    flags = detect_injections({chunk.citation_id: chunk.text for chunk in chunks})

    assert flags == []


def test_the_poisoned_runbook_does_trip_the_injection_detector() -> None:
    from opspilot.agents.opspilot.guardrails import detect_injections

    text = (runbook_dir_for(poisoned_case()) / "connection-pool.md").read_text()
    flags = detect_injections({"connection-pool.md#symptoms": text})

    patterns = {flag.pattern for flag in flags}
    assert "instruction-override" in patterns
    assert "spoofed-role" in patterns
    assert "auto-approval" in patterns
    assert "concealment" in patterns


def test_the_retrieval_benchmark_produces_a_number_and_names_its_misses() -> None:
    from pathlib import Path

    result = bench(load_runbooks(Path(__file__).resolve().parents[2] / "runbooks"))

    assert result["queries"] == len(PARAPHRASE_QUERIES)
    assert 0.0 <= float(result["hit_at_1_rate"]) <= 1.0  # type: ignore[arg-type]
    assert 0.0 <= float(result["hit_at_k_rate"]) <= 1.0  # type: ignore[arg-type]
    assert result["method"]
    assert isinstance(result["misses"], list)
    # The bench is deliberately unkind: at least one query shares no vocabulary with its runbook.
    assert any(query.note for query in PARAPHRASE_QUERIES)


async def test_grade_kinds_are_what_the_gate_says_they_are() -> None:
    config = SuiteConfig(
        provider_factory=reference_factory, provider_label="reference", include_retrieval_bench=False
    )

    report = await run_suite(build_dataset()[:1], config)
    kinds = {grade.name: grade.kind for grade in report.cases[0].grades}

    assert kinds[GradeName.CITATIONS_GROUNDED] is GradeKind.STRUCTURAL
    assert kinds[GradeName.ROOT_CAUSE_MATCH] is GradeKind.SEMANTIC
    assert kinds[GradeName.EXPECTED_TOOLS_USED] is GradeKind.SEMANTIC


@pytest.mark.parametrize("case_id", ["rec-latency-bad-deploy", "rec-error-spike", "rec-cpu-saturation"])
async def test_each_case_can_be_run_in_isolation(case_id: str) -> None:
    config = SuiteConfig(
        provider_factory=reference_factory, provider_label="reference", include_retrieval_bench=False
    )
    cases = [case for case in build_dataset() if case.case_id == case_id]

    report = await run_suite(cases, config)

    assert report.structural_gate_passed, report.summary()["failed_cases"]
