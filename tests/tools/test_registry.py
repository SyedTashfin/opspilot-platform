from __future__ import annotations

import pytest
from pydantic import BaseModel

from opspilot.domain.enums import PermissionClass, RiskLevel
from opspilot.tools.errors import ToolAlreadyRegistered, ToolNotFound
from opspilot.tools.registry import ToolRegistry
from opspilot.tools.types import ToolContext, ToolDefinition


class LogQuery(BaseModel):
    service: str
    minutes: int = 15


class LogResult(BaseModel):
    lines: list[str]


async def _handler(arguments: LogQuery, context: ToolContext) -> LogResult:
    return LogResult(lines=[f"{arguments.service}:{context.agent}"])


def definition(
    name: str = "azure.query_logs",
    *,
    permission: PermissionClass = PermissionClass.READ_ONLY,
    risk: RiskLevel = RiskLevel.LOW,
    timeout: float = 30.0,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Query service logs for a window.",
        handler=_handler,
        input_model=LogQuery,
        output_model=LogResult,
        permission_class=permission,
        risk_level=risk,
        timeout_seconds=timeout,
    )


def test_register_then_get() -> None:
    registry = ToolRegistry([definition()])
    assert registry.get("azure.query_logs").name == "azure.query_logs"
    assert len(registry) == 1
    assert "azure.query_logs" in registry


def test_duplicate_registration_is_refused() -> None:
    registry = ToolRegistry([definition()])
    with pytest.raises(ToolAlreadyRegistered):
        registry.register(definition())


def test_unknown_tool_raises() -> None:
    registry = ToolRegistry()
    with pytest.raises(ToolNotFound):
        registry.get("azure.restart_service")


def test_names_must_be_namespaced() -> None:
    with pytest.raises(ValueError, match="must be namespaced"):
        definition(name="query_logs")


def test_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive timeout"):
        definition(timeout=0)


def test_only_write_restricted_requires_approval() -> None:
    assert definition(permission=PermissionClass.READ_ONLY).requires_approval is False
    assert definition(permission=PermissionClass.WRITE_SAFE).requires_approval is False
    assert definition(permission=PermissionClass.WRITE_RESTRICTED).requires_approval is True


def test_admin_tools_are_never_agent_callable() -> None:
    admin = definition(name="azure.delete_resource_group", permission=PermissionClass.ADMIN)
    assert admin.agent_callable is False
    assert admin.requires_approval is False  # not "approvable" — simply out of reach


def test_for_agent_returns_everything_when_no_allowlist_is_given() -> None:
    registry = ToolRegistry([definition(), definition(name="github.get_commit")])
    assert [d.name for d in registry.for_agent(None)] == ["azure.query_logs", "github.get_commit"]


def test_for_agent_rejects_unregistered_names() -> None:
    registry = ToolRegistry([definition()])
    with pytest.raises(ToolNotFound, match="unregistered tools"):
        registry.for_agent(["azure.query_logs", "azure.typo_tool"])


def test_for_agent_never_widens_beyond_the_allowlist() -> None:
    registry = ToolRegistry([definition(), definition(name="github.get_commit")])
    assert [d.name for d in registry.for_agent(["github.get_commit"])] == ["github.get_commit"]


def test_llm_tools_exclude_admin_tools_and_carry_governance_metadata() -> None:
    registry = ToolRegistry(
        [
            definition(),
            definition(name="azure.restart_service", permission=PermissionClass.WRITE_RESTRICTED),
            definition(name="azure.delete_resource_group", permission=PermissionClass.ADMIN),
        ]
    )
    tools = {tool["name"]: tool for tool in registry.llm_tools()}
    assert "azure.delete_resource_group" not in tools
    assert tools["azure.restart_service"]["opspilot"]["requires_approval"] is True
    assert tools["azure.query_logs"]["opspilot"]["permission_class"] == "read_only"
    assert "properties" in tools["azure.query_logs"]["parameters"]


def test_governance_snapshot_is_sorted_and_complete() -> None:
    registry = ToolRegistry([definition(name="github.get_commit"), definition()])
    snapshot = registry.governance_snapshot()
    assert [entry["name"] for entry in snapshot] == ["azure.query_logs", "github.get_commit"]
    assert set(snapshot[0]) >= {
        "name",
        "description",
        "permission_class",
        "risk_level",
        "requires_approval",
        "agent_callable",
        "timeout_seconds",
        "input_schema",
    }
