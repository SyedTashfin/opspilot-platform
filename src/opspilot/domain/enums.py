"""Enumerations that encode platform policy.

Stored as plain ``VARCHAR(32)`` holding the lowercase value (``native_enum=False``) rather than as
PostgreSQL native enums, and without a DB-level CHECK: adding a status later is then a code change
instead of an ``ALTER TYPE`` migration, which matters for statuses that will grow.
"""

from __future__ import annotations

from enum import StrEnum

#: Placeholder step name for a model call that was not issued by a named pipeline step.
UNSPECIFIED_STEP = "unspecified"


class AgentStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    # Added in M4: the run suspended because a restricted tool needs a human decision.
    WAITING_APPROVAL = "waiting_approval"


class CallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    REJECTED = "rejected"
    NOT_EXECUTED = "not_executed"
    # Added in M3: a restricted tool was requested with no usable approval. Statuses are plain
    # VARCHAR(32) with no CHECK, so this required no migration — the payoff of that choice.
    WAITING_APPROVAL = "waiting_approval"


class PermissionClass(StrEnum):
    """Tool authorisation classes. Everything defaults to the most restrictive useful option."""

    READ_ONLY = "read_only"
    WRITE_SAFE = "write_safe"
    WRITE_RESTRICTED = "write_restricted"
    ADMIN = "admin"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
