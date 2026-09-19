from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from opspilot.domain.enums import CallStatus, PermissionClass, RiskLevel
from opspilot.tools.audit import InMemoryAuditRecorder
from opspilot.tools.errors import ToolNotFound
from opspilot.tools.executor import ToolExecutor
from opspilot.tools.registry import ToolRegistry
from opspilot.tools.types import ToolContext, ToolDefinition, arguments_hash


class RestartArgs(BaseModel):
    service: str
    reason: str


class RestartResult(BaseModel):
    restarted: bool


class QueryArgs(BaseModel):
    service: str


class QueryResult(BaseModel):
    lines: list[str]


CALLS: list[str] = []


async def _ok_query(arguments: QueryArgs, context: ToolContext) -> QueryResult:
    CALLS.append(arguments.service)
    return QueryResult(lines=["one line"])


async def _slow_query(arguments: QueryArgs, context: ToolContext) -> QueryResult:
    import asyncio

    await asyncio.sleep(0.3)
    return QueryResult(lines=[])


async def _raising_query(arguments: QueryArgs, context: ToolContext) -> QueryResult:
    raise RuntimeError("telemetry backend unreachable")


async def _silent_restart(arguments: RestartArgs, context: ToolContext) -> RestartResult:
    CALLS.append(arguments.service)
    return RestartResult(restarted=True)


async def _null_output(arguments: QueryArgs, context: ToolContext) -> None:
    return None


@dataclass
class StubApprovals:
    """Single-use approval bound to one tool and one argument hash."""

    token: str
    tool_name: str
    arguments_hash: str
    remaining: int = 1

    async def consume(self, *, token: str, tool_name: str, arguments_hash: str) -> bool:
        if (
            token != self.token
            or tool_name != self.tool_name
            or arguments_hash != self.arguments_hash
            or self.remaining <= 0
        ):
            return False
        self.remaining -= 1
        return True


def read_tool(handler: object = _ok_query, timeout: float = 5.0) -> ToolDefinition:
    return ToolDefinition(
        name="azure.query_logs",
        description="Query logs",
        handler=handler,  # type: ignore[arg-type]
        input_model=QueryArgs,
        output_model=QueryResult,
        permission_class=PermissionClass.READ_ONLY,
        risk_level=RiskLevel.LOW,
        timeout_seconds=timeout,
    )


def write_tool() -> ToolDefinition:
    return ToolDefinition(
        name="azure.restart_service",
        description="Restart a service",
        handler=_silent_restart,
        input_model=RestartArgs,
        output_model=RestartResult,
        permission_class=PermissionClass.WRITE_RESTRICTED,
        risk_level=RiskLevel.HIGH,
        timeout_seconds=5.0,
    )


def admin_tool() -> ToolDefinition:
    return ToolDefinition(
        name="azure.delete_resource_group",
        description="Delete a resource group",
        handler=_silent_restart,
        input_model=RestartArgs,
        permission_class=PermissionClass.ADMIN,
        risk_level=RiskLevel.CRITICAL,
        timeout_seconds=5.0,
    )


def build(
    *tools: ToolDefinition, approvals: StubApprovals | None = None
) -> tuple[ToolExecutor, InMemoryAuditRecorder]:
    audit = InMemoryAuditRecorder()
    executor = ToolExecutor(registry=ToolRegistry(tools), audit=audit, approvals=approvals)
    return executor, audit


CONTEXT = ToolContext(agent="opspilot", run_id=None)


@pytest.fixture(autouse=True)
def _clear_calls() -> None:
    CALLS.clear()


async def test_read_only_tool_executes_without_approval() -> None:
    executor, audit = build(read_tool())
    outcome = await executor.execute("azure.query_logs", {"service": "api"}, CONTEXT)
    assert outcome.executed is True
    assert outcome.status is CallStatus.OK
    assert outcome.output == QueryResult(lines=["one line"])
    assert CALLS == ["api"]
    assert "tool.executed" in audit.actions()
    assert audit.verify_chain() is True


async def test_invalid_arguments_never_reach_the_handler() -> None:
    executor, audit = build(read_tool())
    outcome = await executor.execute("azure.query_logs", {"service_name": "api"}, CONTEXT)
    assert outcome.status is CallStatus.ERROR
    assert outcome.output is None
    assert CALLS == []
    assert "tool.error" in audit.actions()


async def test_restricted_tool_without_approval_waits_for_a_human() -> None:
    executor, audit = build(write_tool())
    outcome = await executor.execute(
        "azure.restart_service", {"service": "api", "reason": "latency"}, CONTEXT
    )
    assert outcome.status is CallStatus.WAITING_APPROVAL
    assert CALLS == []
    assert "tool.waiting_approval" in audit.actions()


async def test_restricted_tool_executes_with_a_matching_approval() -> None:
    arguments = {"service": "api", "reason": "latency"}
    approvals = StubApprovals(
        token="tok-1", tool_name="azure.restart_service", arguments_hash=arguments_hash(arguments)
    )
    executor, audit = build(write_tool(), approvals=approvals)

    outcome = await executor.execute(
        "azure.restart_service", arguments, CONTEXT, approval_token="tok-1"
    )

    assert outcome.status is CallStatus.OK
    assert CALLS == ["api"]
    assert "approval.consumed" in audit.actions()


async def test_approval_is_single_use() -> None:
    arguments = {"service": "api", "reason": "latency"}
    approvals = StubApprovals(
        token="tok-1", tool_name="azure.restart_service", arguments_hash=arguments_hash(arguments)
    )
    executor, _ = build(write_tool(), approvals=approvals)

    first = await executor.execute(
        "azure.restart_service", arguments, CONTEXT, approval_token="tok-1"
    )
    second = await executor.execute(
        "azure.restart_service", arguments, CONTEXT, approval_token="tok-1"
    )

    assert first.status is CallStatus.OK
    assert second.status is CallStatus.REJECTED
    assert CALLS == ["api"]


async def test_approval_bound_to_other_arguments_is_refused() -> None:
    approved_args = {"service": "api", "reason": "latency"}
    different_args = {"service": "database", "reason": "latency"}
    approvals = StubApprovals(
        token="tok-1",
        tool_name="azure.restart_service",
        arguments_hash=arguments_hash(approved_args),
    )
    executor, _ = build(write_tool(), approvals=approvals)

    outcome = await executor.execute(
        "azure.restart_service", different_args, CONTEXT, approval_token="tok-1"
    )

    assert outcome.status is CallStatus.REJECTED
    assert CALLS == []


async def test_admin_tool_is_refused_even_with_an_approval() -> None:
    arguments = {"service": "api", "reason": "cleanup"}
    approvals = StubApprovals(
        token="tok-1",
        tool_name="azure.delete_resource_group",
        arguments_hash=arguments_hash(arguments),
    )
    executor, audit = build(admin_tool(), approvals=approvals)

    outcome = await executor.execute(
        "azure.delete_resource_group", arguments, CONTEXT, approval_token="tok-1"
    )

    assert outcome.status is CallStatus.REJECTED
    assert "not callable by agents" in (outcome.error or "")
    assert CALLS == []
    assert "tool.rejected" in audit.actions()


async def test_handler_timeout_is_an_outcome_not_a_crash() -> None:
    executor, audit = build(read_tool(handler=_slow_query, timeout=0.01))
    outcome = await executor.execute("azure.query_logs", {"service": "api"}, CONTEXT)
    assert outcome.status is CallStatus.TIMEOUT
    assert "timeout" in (outcome.error or "")
    assert "tool.timeout" in audit.actions()


async def test_handler_exception_is_contained_and_audited() -> None:
    executor, audit = build(read_tool(handler=_raising_query))
    outcome = await executor.execute("azure.query_logs", {"service": "api"}, CONTEXT)
    assert outcome.status is CallStatus.ERROR
    assert "RuntimeError" in (outcome.error or "")
    assert "tool.error" in audit.actions()


async def test_declared_output_schema_is_enforced() -> None:
    executor, _ = build(read_tool(handler=_null_output))
    outcome = await executor.execute("azure.query_logs", {"service": "api"}, CONTEXT)
    assert outcome.status is CallStatus.ERROR
    assert "no output" in (outcome.error or "")


async def test_unknown_tool_is_audited_and_raised() -> None:
    executor, audit = build(read_tool())
    with pytest.raises(ToolNotFound):
        await executor.execute("azure.typo_tool", {}, CONTEXT)
    assert "tool.unknown" in audit.actions()
    assert audit.verify_chain() is True
