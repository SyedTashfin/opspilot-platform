"""Command line entry point: ``uv run python -m opspilot.evals run``.

Three provider choices, and the difference between them is the whole point:

* ``reference`` — deterministic, answers from the case's own answer key. Runs in CI. Proves the harness
  works: pipeline, grounding, the approval gate, the graders. Its semantic scores are the harness's
  ceiling, not a model's ability, and the output says so.
* ``fake`` — the gateway's deterministic dummy, which answers with a citation to an id that does not
  exist. It should *fail* the citation grade; that is how the graders are shown to detect a bad answer.
* ``configured`` — the models from settings (DeepSeek, Mistral). Without a key the run reports that the
  measurement could not be taken rather than inventing a number.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from opspilot.evals.dataset import build_dataset
from opspilot.evals.providers import ReferenceProvider
from opspilot.evals.runner import EVAL_MODEL, SuiteConfig, run_suite
from opspilot.evals.types import EvalCase, SuiteReport
from opspilot.gateway.providers.base import ModelProvider
from opspilot.gateway.providers.fake import FAKE_MODEL, FakeProvider

REFERENCE_EVIDENCE = ("ev-metrics", "ev-logs")
ROLLBACK_CASE = "rec-latency-bad-deploy"


def reference_factory(case: EvalCase) -> ModelProvider:
    """A grounded answer built from the case's own key. Measures the harness, not a model."""
    tool = "azure.rollback_deployment" if case.case_id == ROLLBACK_CASE else None
    arguments = (
        {
            "service": str(case.alert["service"]),
            "version": "rec-2026.05.9",
            "reason": "the deployment that changed dependency timeouts caused retry amplification",
        }
        if tool
        else {}
    )
    acceptable = case.required_terms or ()
    root_cause = " ; ".join(group[0] for group in acceptable) or (
        case.acceptable_diagnoses[0] if case.acceptable_diagnoses else ""
    )
    return ReferenceProvider(
        evidence_ids=list(REFERENCE_EVIDENCE),
        root_cause=f"the root cause is {root_cause}",
        summary="apply the runbook remediation for this symptom",
        tool=tool,
        arguments=arguments,
    )


def fake_factory(case: EvalCase) -> ModelProvider:
    return FakeProvider()


def configured_factory(case: EvalCase) -> ModelProvider:
    from opspilot.gateway.providers.litellm_provider import LiteLLMProvider

    return LiteLLMProvider()


FACTORIES: dict[str, Callable[[EvalCase], ModelProvider]] = {
    "reference": reference_factory,
    "fake": fake_factory,
    "configured": configured_factory,
}


def model_for(provider_label: str) -> str:
    """The model name the chosen provider actually serves.

    Each provider answers only for the models it claims (``FakeProvider.supports`` refuses anything not
    prefixed ``fake``), so naming the right model is part of selecting the provider rather than a detail.
    """
    if provider_label == "fake":
        return FAKE_MODEL
    if provider_label == "configured":
        from opspilot.config import get_settings

        return get_settings().default_model
    return EVAL_MODEL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opspilot.evals", description="OpsPilot evaluation suite")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run the suite and write a report")
    run.add_argument("--provider", choices=sorted(FACTORIES), default="reference")
    run.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    run.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help=(
            "also gate on the mean semantic score. Off by default: semantic grades come from a model, "
            "and gating on a reference provider's score would measure the harness twice"
        ),
    )
    run.add_argument("--only", nargs="*", default=None, help="restrict to these case ids")
    run.add_argument("--skip-retrieval", action="store_true", help="skip the retrieval benchmark")
    return parser


def _print_report(report: SuiteReport, *, semantic_gated: bool) -> None:
    summary = report.summary()
    print(f"provider          : {report.provider} ({report.model})")
    print(f"cases             : {summary['cases']}")
    print(f"structural gate   : {'PASS' if report.structural_gate_passed else 'FAIL'}")
    print(f"cases passing     : {summary['cases_meeting_structural_grades']}/{summary['cases']}")
    print(
        f"mean semantic     : {summary['mean_semantic_score']}"
        + ("" if semantic_gated else "  (reported, not gated)")
    )
    print(f"cost              : {summary['total_cost_eur']} EUR")
    print(f"duration          : {summary['duration_ms']} ms")
    if report.retrieval:
        retrieval = report.retrieval
        print(
            f"retrieval (BM25)  : hit@1 {retrieval['hit_at_1_rate']}, hit@k {retrieval['hit_at_k_rate']}"
            f" over {retrieval['queries']} paraphrased queries"
        )
        if retrieval["misses"]:
            print(f"  retrieval misses: {retrieval['misses']}")
    for case in report.cases:
        failed = [
            grade.name.value for grade in case.grades if grade.kind.value == "structural" and not grade.passed
        ]
        flags = "flagged" if any("injection" in note for note in case.notes) else "-"
        verdict = "PASS" if case.structural_passed else "FAIL"
        print(
            f"  {case.case_id:<22} {case.run_status:<17} structural={verdict}"
            f" semantic={case.semantic_score} injections={flags}" + (f" failed={failed}" if failed else "")
        )


async def _run(args: argparse.Namespace) -> int:
    cases = build_dataset()
    if args.only:
        wanted = set(args.only)
        cases = [case for case in cases if case.case_id in wanted]
        if not cases:
            print(f"no case matched {sorted(wanted)}", file=sys.stderr)
            return 2
    config = SuiteConfig(
        provider_factory=FACTORIES[args.provider],
        provider_label=args.provider,
        model=model_for(args.provider),
        include_retrieval_bench=not args.skip_retrieval,
    )
    report = await run_suite(cases, config)
    _print_report(report, semantic_gated=args.fail_under is not None)
    if args.provider == "reference":
        print(
            "note: 'reference' answers from the case's own answer key. The numbers above measure the "
            "harness, not a model."
        )
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report.to_json(), indent=2) + "\n")
        print(f"report written to {args.out}")

    if not report.structural_gate_passed:
        return 1
    if args.fail_under is not None and report.summary()["mean_semantic_score"] < args.fail_under:
        print(
            f"mean semantic score {report.summary()['mean_semantic_score']} is below --fail-under "
            f"{args.fail_under}",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # The suite makes a lot of HTTP calls to the lab; their request logs are noise in a report.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return asyncio.run(_run(args))
    return 2  # pragma: no cover - argparse enforces the choices


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
