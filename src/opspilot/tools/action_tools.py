"""Restricted action tools.

These exist so the platform can demonstrate the gate rather than describe it. They are
``WRITE_RESTRICTED`` and ``HIGH`` risk: the executor refuses to run them without a single-use
approval
bound to the exact arguments, and the run suspends with ``waiting_approval`` until a human decides.

The handlers here record the request rather than mutating real infrastructure. When the Incident Lab
ships (M6) they will call the demo service's control API; the governance around them does not
change.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from opspilot.domain.enums import PermissionClass, RiskLevel
from opspilot.tools.types import ToolContext, ToolDefinition


class RestartArgs(BaseModel):
    service: str = Field(description="Service to restart")
    reason: str = Field(min_length=5, max_length=300, description="Why this action is being taken")


class RollbackArgs(BaseModel):
    service: str = Field(description="Service to roll back")
    version: str = Field(description="Version to return to")
    reason: str = Field(min_length=5, max_length=300)


class ActionAccepted(BaseModel):
    service: str
    action: str
    accepted: bool
    note: str


@dataclass
class ActionLog:
    """Records requested actions so the runtime, tests and evaluations can inspect them."""

    entries: list[dict[str, str]] = field(default_factory=list)

    def record(self, **entry: str) -> None:
        self.entries.append(dict(entry))


def action_tools(log: ActionLog) -> list[ToolDefinition]:
    async def restart_service(arguments: RestartArgs, context: ToolContext) -> ActionAccepted:
        log.record(
            action="restart",
            service=arguments.service,
            reason=arguments.reason,
            actor=context.agent,
        )
        return ActionAccepted(
            service=arguments.service,
            action="restart",
            accepted=True,
            note="recorded by the action log; the Incident Lab wires this to real recovery in M6",
        )

    async def rollback_deployment(arguments: RollbackArgs, context: ToolContext) -> ActionAccepted:
        log.record(
            action="rollback",
            service=arguments.service,
            version=arguments.version,
            reason=arguments.reason,
            actor=context.agent,
        )
        return ActionAccepted(
            service=arguments.service,
            action="rollback",
            accepted=True,
            note="recorded by the action log; the Incident Lab wires this to real recovery in M6",
        )

    return [
        ToolDefinition(
            name="azure.restart_service",
            description=("Restart a service. Restricted: requires human approval for these arguments."),
            handler=restart_service,
            input_model=RestartArgs,
            output_model=ActionAccepted,
            permission_class=PermissionClass.WRITE_RESTRICTED,
            risk_level=RiskLevel.HIGH,
            timeout_seconds=60.0,
        ),
        ToolDefinition(
            name="azure.rollback_deployment",
            description=("Roll a service back to a previous version. Restricted: needs human approval."),
            handler=rollback_deployment,
            input_model=RollbackArgs,
            output_model=ActionAccepted,
            permission_class=PermissionClass.WRITE_RESTRICTED,
            risk_level=RiskLevel.HIGH,
            timeout_seconds=120.0,
        ),
    ]
