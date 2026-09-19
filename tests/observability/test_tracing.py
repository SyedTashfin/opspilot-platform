"""Tracing: the span structure of a run, and what spans are not allowed to contain.

Structure is asserted through parent links rather than by counting, because the useful question is not
"how many spans" but "is this span where a reader will look for it". The prompt-content test is the one
that matters most: the gateway sees prompts, and a trace must not become a leak path for them.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from opspilot.agent.runtime import AgentRuntime
from opspilot.agent.store import InMemoryRunStore
from opspilot.agent.types import RunLimits, RunState, StepResult
from opspilot.domain.enums import RunStatus
from opspilot.gateway.accounting import InMemoryLedger, InMemoryRecorder
from opspilot.gateway.gateway import GatewayConfig, ModelGateway
from opspilot.gateway.policy import ModelChain, ModelPolicy
from opspilot.gateway.providers.fake import FakeProvider
from opspilot.gateway.types import Message, ModelRequest
from opspilot.observability.spans import COLLECTOR, SpanRecord
from opspilot.observability.tracing import OtelStepTracer, configure_tracing
from opspilot.telemetry.scenarios import get_scenario
from opspilot.telemetry.synthetic import SyntheticTelemetrySource
from opspilot.tools.audit import InMemoryAuditRecorder
from opspilot.tools.executor import ToolExecutor
from opspilot.tools.registry import ToolRegistry
from opspilot.tools.telemetry_tools import telemetry_tools
from opspilot.tools.types import ToolContext

PROMPT_SENTINEL = "PROMPT-SENTINEL-9137"
MODEL = "fake/fake-1"
SCENARIO = get_scenario("rec-latency-bad-deploy")


@pytest.fixture(autouse=True)
def _tracing() -> Iterator[None]:
    """A real tracer provider for the whole test module, with an empty buffer per test."""
    configure_tracing("opspilot-test", environment="ci")
    COLLECTOR.drain()
    yield
    COLLECTOR.drain()


@dataclass
class StubStep:
    name: str
    outcome: str = "succeed"

    async def run(self, state: RunState) -> StepResult:
        if self.outcome == "fail":
            return StepResult.failed(f"{self.name} failed")
        if self.outcome == "wait":
            return StepResult.waiting_approval(f"{self.name} needs approval")
        return StepResult.succeeded(f"{self.name} ok")


class ModelStep:
    """A step whose whole job is to call the gateway, so the nesting can be observed."""

    name = "diagnose"

    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway

    async def run(self, state: RunState) -> StepResult:
        await self.gateway.complete(
            ModelRequest(step="diagnose", messages=(Message.user(PROMPT_SENTINEL),)),
            state.run_id,
        )
        return StepResult.succeeded("called the model")


class ToolStep:
    """A step that runs a real tool through the executor."""

    name = "collect_metrics"

    def __init__(self, executor: ToolExecutor) -> None:
        self.executor = executor

    async def run(self, state: RunState) -> StepResult:
        outcome = await self.executor.execute(
            "azure.get_metrics",
            {"service": SCENARIO.service, "window_minutes": 30},
            ToolContext(agent="opspilot", run_id=state.run_id),
        )
        return StepResult.succeeded(f"read metrics ({outcome.status.value})")


def gateway() -> ModelGateway:
    return ModelGateway(
        providers=[FakeProvider()],
        recorder=InMemoryRecorder(),
        ledger=InMemoryLedger(),
        config=GatewayConfig(
            policy=ModelPolicy(default_chain=ModelChain(step="default", models=(MODEL,)), step_chains={}),
            run_cost_cap_eur=1.0,
            daily_cost_cap_eur=5.0,
        ),
    )


def build() -> tuple[AgentRuntime, InMemoryRunStore]:
    store = InMemoryRunStore()
    store.register_agent("opspilot")
    runtime = AgentRuntime(
        store=store,
        ledger=InMemoryLedger(),
        limits=RunLimits(),
        tracer=OtelStepTracer(),
    )
    return runtime, store


def by_name(spans: list[SpanRecord], name: str) -> SpanRecord:
    for record in spans:
        if record.name == name:
            return record
    msg = f"no span named {name!r} in {[record.name for record in spans]}"
    raise AssertionError(msg)


async def test_a_run_produces_a_run_span_with_one_child_per_step() -> None:
    runtime, store = build()
    steps = [StubStep("classify"), StubStep("diagnose")]

    summary = await runtime.execute(agent="opspilot", request={"symptom": "latency"}, steps=steps)

    spans = store.spans[summary.run_id]
    run_span = by_name(spans, "agent.run")
    step_spans = [record for record in spans if record.name.startswith("agent.step.")]

    assert run_span.parent_span_id is None
    assert [record.name for record in step_spans] == ["agent.step.classify", "agent.step.diagnose"]
    assert all(record.parent_span_id == run_span.span_id for record in step_spans)
    assert {record.trace_id for record in spans} == {run_span.trace_id}
    assert run_span.attributes["run.status"] == RunStatus.SUCCEEDED.value
    assert run_span.attributes["run.steps_executed"] == 2
    assert run_span.attributes["agent.name"] == "opspilot"


async def test_the_run_row_records_its_trace_id_and_the_spans_are_persisted() -> None:
    runtime, store = build()

    summary = await runtime.execute(agent="opspilot", request={}, steps=[StubStep("classify")])

    assert store.runs[summary.run_id]["trace_id"]
    assert len(store.spans[summary.run_id]) == 2


async def test_a_failed_run_is_an_error_span() -> None:
    runtime, store = build()

    summary = await runtime.execute(
        agent="opspilot", request={}, steps=[StubStep("classify", outcome="fail")]
    )

    run_span = by_name(store.spans[summary.run_id], "agent.run")
    assert summary.status is RunStatus.FAILED
    assert run_span.status == "error"
    assert run_span.attributes["run.failed"] is True
    assert run_span.attributes["error.type"] == RunStatus.FAILED.value


async def test_suspending_for_approval_is_not_an_error_span() -> None:
    runtime, store = build()

    summary = await runtime.execute(
        agent="opspilot", request={}, steps=[StubStep("propose_remediation", outcome="wait")]
    )

    run_span = by_name(store.spans[summary.run_id], "agent.run")
    assert summary.status is RunStatus.WAITING_APPROVAL
    assert run_span.status == "ok"


async def test_a_model_call_nests_under_the_step_that_made_it() -> None:
    runtime, store = build()

    summary = await runtime.execute(agent="opspilot", request={}, steps=[ModelStep(gateway())])

    spans = store.spans[summary.run_id]
    run_span = by_name(spans, "agent.run")
    step_span = by_name(spans, "agent.step.diagnose")
    model_span = by_name(spans, "gateway.complete")

    assert model_span.parent_span_id == step_span.span_id
    assert step_span.parent_span_id == run_span.span_id
    assert model_span.attributes["llm.model"] == MODEL
    assert model_span.attributes["llm.step"] == "diagnose"
    assert model_span.attributes["llm.input_tokens"] > 0
    assert model_span.attributes["llm.cost_known"] is True


async def test_a_tool_call_span_records_its_governance() -> None:
    registry = ToolRegistry(telemetry_tools(SyntheticTelemetrySource(scenario=SCENARIO)))
    executor = ToolExecutor(registry=registry, audit=InMemoryAuditRecorder())
    runtime, store = build()

    summary = await runtime.execute(agent="opspilot", request={}, steps=[ToolStep(executor)])

    spans = store.spans[summary.run_id]
    tool_span = by_name(spans, "tool.execute")
    step_span = by_name(spans, "agent.step.collect_metrics")

    assert tool_span.parent_span_id == step_span.span_id
    assert tool_span.attributes["tool.name"] == "azure.get_metrics"
    assert tool_span.attributes["tool.permission_class"] == "read_only"
    assert tool_span.attributes["tool.risk_level"] == "low"
    assert tool_span.attributes["tool.status"] == "ok"
    assert tool_span.attributes["run.id"]
    assert tool_span.attributes["tool.arguments_hash"]


async def test_spans_never_carry_prompt_content() -> None:
    """The gateway sees prompts and completions. A trace must not become a leak path for them."""
    runtime, store = build()

    summary = await runtime.execute(agent="opspilot", request={}, steps=[ModelStep(gateway())])

    for record in store.spans[summary.run_id]:
        flat = " ".join(f"{key}={value}" for key, value in record.attributes.items())
        assert PROMPT_SENTINEL not in flat
        assert PROMPT_SENTINEL not in record.name


async def test_tool_and_model_attributes_stay_json_scalars() -> None:
    runtime, store = build()

    summary = await runtime.execute(agent="opspilot", request={}, steps=[ModelStep(gateway())])

    for record in store.spans[summary.run_id]:
        for key, value in record.attributes.items():
            assert isinstance(value, str | int | float | bool), (record.name, key, type(value))


def test_span_records_are_plain_data() -> None:
    """SpanRecord carries no OpenTelemetry types, so the store and tests can build one directly."""
    from datetime import datetime
    from typing import get_type_hints

    hints = get_type_hints(SpanRecord)
    assert hints["started_at"] is datetime
    assert hints["ended_at"] is datetime
    assert hints["attributes"] == dict[str, Any]
    import dataclasses

    assert {field.name for field in dataclasses.fields(SpanRecord)} == {
        "trace_id",
        "span_id",
        "parent_span_id",
        "name",
        "kind",
        "started_at",
        "ended_at",
        "duration_ms",
        "status",
        "attributes",
    }


def test_a_span_record_row_carries_every_column_the_table_expects() -> None:
    from datetime import UTC, datetime

    record = SpanRecord(
        trace_id="a" * 32,
        span_id="b" * 16,
        parent_span_id=None,
        name="agent.run",
        kind="internal",
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        duration_ms=1,
        status="ok",
        attributes={"run.status": "succeeded"},
    )

    row: dict[str, Any] = record.as_row(None)

    assert set(row) == {
        "id",
        "run_id",
        "trace_id",
        "span_id",
        "parent_span_id",
        "name",
        "kind",
        "status",
        "started_at",
        "ended_at",
        "duration_ms",
        "attributes",
    }
    assert row["run_id"] is None
