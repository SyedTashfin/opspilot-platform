"""Core schema.

Scope of M1: the tables the platform spine needs (agents, runs, steps, model calls, tool calls,
approvals, audit events). Evaluation, incident-lab and runbook tables arrive with their milestones
through new migrations — never through ad-hoc DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from opspilot.db.base import Base
from opspilot.domain.enums import (
    UNSPECIFIED_STEP,
    AgentStatus,
    ApprovalDecision,
    CallStatus,
    PermissionClass,
    RiskLevel,
    RunStatus,
    StepStatus,
)

# JSONB on PostgreSQL, plain JSON elsewhere so the schema is testable without a server.
JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def enum_column(enum_cls: type[StrEnum]) -> sa.Enum:
    """VARCHAR + application-side validation, storing the enum *values* (lowercase).

    ``native_enum=False`` keeps the schema portable and testable; ``create_constraint=False`` is
    deliberate — a DB-level CHECK would have to be migrated in lockstep every time a status gains a
    value, for no safety gain here because all writes go through the ORM with ``validate_strings``.
    """
    return sa.Enum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=32,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


class Agent(Base, TimestampMixin):
    """An agent definition: what it may do, with which models, under which evaluation suite."""

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(sa.String(120), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    status: Mapped[AgentStatus] = mapped_column(
        enum_column(AgentStatus), nullable=False, default=AgentStatus.DRAFT
    )
    model_policy: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    allowed_tools: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    permissions: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    evaluation_suite: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)


class Run(Base):
    """One execution of one agent. Carries its own budget and trace identifier."""

    __tablename__ = "runs"
    __table_args__ = (sa.Index("ix_runs_agent_created", "agent_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[RunStatus] = mapped_column(
        enum_column(RunStatus), nullable=False, default=RunStatus.PENDING
    )
    trigger: Mapped[str | None] = mapped_column(sa.String(60), nullable=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    model: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    input_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    cost_eur: Mapped[float] = mapped_column(
        sa.Numeric(12, 6), nullable=False, default=0, server_default="0"
    )
    trace_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class RunStep(Base):
    """Operational trace of a run: what was done, with what, and what came back.

    Deliberately *not* a chain-of-thought store: steps record actions, observations and decisions.
    """

    __tablename__ = "run_steps"
    __table_args__ = (sa.UniqueConstraint("run_id", "step_index", name="uq_run_steps_run_id_step"),)

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    status: Mapped[StepStatus] = mapped_column(
        enum_column(StepStatus), nullable=False, default=StepStatus.PENDING
    )
    summary: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)


class ModelCall(Base):
    """One model request. The row that makes cost and latency claims checkable."""

    __tablename__ = "model_calls"
    __table_args__ = (sa.Index("ix_model_calls_step", "step"),)

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(sa.String(40), nullable=False)
    model: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    # The pipeline step that issued the call ("classify", "diagnose", ...). Without it, a run's cost
    # is a total with no attribution.
    step: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, default=UNSPECIFIED_STEP, server_default=UNSPECIFIED_STEP
    )
    request_id: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    input_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    cost_eur: Mapped[float] = mapped_column(
        sa.Numeric(12, 6), nullable=False, default=0, server_default="0"
    )
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    status: Mapped[CallStatus] = mapped_column(
        enum_column(CallStatus), nullable=False, default=CallStatus.OK
    )
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class Approval(Base):
    """Human decision gate for a restricted action: single use, expiring, bound to arguments."""

    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    token: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    decision: Mapped[ApprovalDecision] = mapped_column(
        enum_column(ApprovalDecision), nullable=False, default=ApprovalDecision.PENDING
    )
    requested_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)


class ToolCall(Base):
    """Every tool execution leaves a row, including the ones that were refused."""

    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    permission_class: Mapped[PermissionClass] = mapped_column(
        enum_column(PermissionClass), nullable=False, default=PermissionClass.READ_ONLY
    )
    risk_level: Mapped[RiskLevel] = mapped_column(
        enum_column(RiskLevel), nullable=False, default=RiskLevel.LOW
    )
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    result_summary: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    status: Mapped[CallStatus] = mapped_column(
        enum_column(CallStatus), nullable=False, default=CallStatus.NOT_EXECUTED
    )
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("approvals.id", ondelete="SET NULL"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class AuditEvent(Base):
    """Append-only record; ``prev_hash``/``hash`` enable tamper-evidence in a later milestone."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    seq: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), nullable=False, unique=True)
    at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    actor: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    action: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    subject: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    prev_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
