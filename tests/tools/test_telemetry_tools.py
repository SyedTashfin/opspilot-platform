from __future__ import annotations

from pathlib import Path

import pytest

from opspilot.domain.enums import CallStatus
from opspilot.retrieval.runbooks import load_runbooks
from opspilot.telemetry.scenarios import get_scenario
from opspilot.telemetry.synthetic import SyntheticTelemetrySource
from opspilot.tools.action_tools import ActionLog, action_tools
from opspilot.tools.audit import InMemoryAuditRecorder
from opspilot.tools.errors import ToolNotFound
from opspilot.tools.executor import ToolExecutor
from opspilot.tools.registry import ToolRegistry
from opspilot.tools.runbook_tools import runbook_tools
from opspilot.tools.telemetry_tools import (
    DeploymentsResult,
    LogQueryResult,
    MetricsResult,
    ResourceStateResult,
    telemetry_tools,
)
from opspilot.tools.types import ToolContext

RUNBOOK_DIR = Path(__file__).resolve().parents[2] / "runbooks"
SCENARIO = get_scenario("rec-latency-bad-deploy")
SERVICE = SCENARIO.service
CONTEXT = ToolContext(agent="opspilot")


@pytest.fixture
def harness() -> tuple[ToolExecutor, ActionLog]:
    source = SyntheticTelemetrySource(scenario=SCENARIO)
    actions = ActionLog()
    registry = ToolRegistry(
        telemetry_tools(source) + runbook_tools(load_runbooks(RUNBOOK_DIR)) + action_tools(actions)
    )
    return ToolExecutor(registry=registry, audit=InMemoryAuditRecorder()), actions


async def test_read_only_telemetry_tools_execute_without_approval(harness) -> None:
    executor, _ = harness

    metrics = await executor.execute("azure.get_metrics", {"service": SERVICE}, CONTEXT)
    logs = await executor.execute("azure.query_logs", {"service": SERVICE, "limit": 5}, CONTEXT)
    deployments = await executor.execute(
        "github.get_recent_deployments", {"service": SERVICE}, CONTEXT
    )
    resource = await executor.execute("azure.get_resource_state", {"service": SERVICE}, CONTEXT)

    assert metrics.status is CallStatus.OK
    assert isinstance(metrics.output, MetricsResult)
    assert isinstance(logs.output, LogQueryResult) and len(logs.output.lines) == 5
    assert isinstance(deployments.output, DeploymentsResult) and deployments.output.deployments
    assert isinstance(resource.output, ResourceStateResult)
    assert metrics.output.source == "demo"
    assert metrics.output.series["request_latency_p95_ms"].p95 > 0


async def test_log_tool_passes_filters_through(harness) -> None:
    executor, _ = harness

    outcome = await executor.execute(
        "azure.query_logs",
        {"service": SERVICE, "level": "WARN", "contains": "timeout"},
        CONTEXT,
    )

    assert outcome.status is CallStatus.OK
    assert outcome.output is not None
    assert outcome.output.lines
    assert all(line.level == "WARN" for line in outcome.output.lines)


async def test_runbook_tool_returns_citable_chunks(harness) -> None:
    executor, _ = harness

    outcome = await executor.execute(
        "docs.search_runbook", {"query": "retry amplification slow dependency", "limit": 2}, CONTEXT
    )

    assert outcome.status is CallStatus.OK
    assert outcome.output is not None
    assert 0 < len(outcome.output.chunks) <= 2
    assert all(chunk.citation_id and chunk.content_hash for chunk in outcome.output.chunks)


async def test_restricted_actions_are_refused_and_audited(harness) -> None:
    executor, actions = harness

    outcome = await executor.execute(
        "azure.restart_service",
        {"service": SERVICE, "reason": "rollback of a bad deployment"},
        CONTEXT,
    )

    assert outcome.status is CallStatus.WAITING_APPROVAL
    assert outcome.output is None
    assert actions.entries == []
    assert "azure.restart_service" in executor.audit.subjects()


async def test_invalid_arguments_never_reach_the_handler(harness) -> None:
    executor, actions = harness

    outcome = await executor.execute("azure.restart_service", {"service": SERVICE}, CONTEXT)

    assert outcome.status is not CallStatus.OK
    assert outcome.output is None
    assert outcome.error is not None
    assert actions.entries == []


async def test_an_agent_allowlist_restricts_the_registry(harness) -> None:
    """An agent sees, and can call, only the tools it was granted."""
    executor, _ = harness

    allowed = executor.registry.for_agent(["azure.get_metrics"])
    assert [definition.name for definition in allowed] == ["azure.get_metrics"]

    restricted = ToolExecutor(registry=ToolRegistry(allowed), audit=InMemoryAuditRecorder())
    assert await restricted.execute("azure.get_metrics", {"service": SERVICE}, CONTEXT)
    with pytest.raises(ToolNotFound):
        await restricted.execute("azure.query_logs", {"service": SERVICE}, CONTEXT)


def test_the_allowlist_may_not_name_a_tool_that_does_not_exist(harness) -> None:
    executor, _ = harness

    with pytest.raises(ToolNotFound):
        executor.registry.for_agent(["azure.get_metrics", "does.not.exist"])


async def test_telemetry_tools_declare_their_governance(harness) -> None:
    executor, _ = harness

    snapshot = {row["name"]: row for row in executor.registry.governance_snapshot()}

    assert snapshot["azure.get_metrics"]["permission_class"] == "read_only"
    assert snapshot["azure.restart_service"]["permission_class"] == "write_restricted"
    assert snapshot["azure.restart_service"]["risk_level"] == "high"
