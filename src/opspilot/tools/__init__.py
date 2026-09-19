"""Tool layer: definitions, registry, permission gate, audit trail."""

from opspilot.tools.audit import (
    AuditEvent,
    AuditRecorder,
    InMemoryAuditRecorder,
    PostgresAuditRecorder,
)
from opspilot.tools.errors import (
    ApprovalDenied,
    ToolAlreadyRegistered,
    ToolError,
    ToolNotFound,
)
from opspilot.tools.executor import DenyAllApprovals, ToolExecutor
from opspilot.tools.registry import ToolRegistry
from opspilot.tools.types import ToolContext, ToolDefinition, ToolOutcome, arguments_hash

__all__ = [
    "ApprovalDenied",
    "AuditEvent",
    "AuditRecorder",
    "DenyAllApprovals",
    "InMemoryAuditRecorder",
    "PostgresAuditRecorder",
    "ToolAlreadyRegistered",
    "ToolContext",
    "ToolDefinition",
    "ToolError",
    "ToolExecutor",
    "ToolNotFound",
    "ToolOutcome",
    "ToolRegistry",
    "arguments_hash",
]
