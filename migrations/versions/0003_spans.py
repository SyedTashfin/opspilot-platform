"""spans: persist OpenTelemetry spans so a trace outlives the process

Revision ID: 0003_spans
Revises: 0002_model_call_step
Create Date: 2026-09-19

Spans are written once, at the end of a run, by the run store — no background span processor writing
to a second connection mid-run. ``trace_id`` and ``run_id`` are indexed because they are the two ways
a trace is read back.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_spans"
down_revision = "0002_model_call_step"
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "spans",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE", name="fk_spans_run_id_runs"),
            nullable=True,
        ),
        sa.Column("trace_id", sa.String(32), nullable=False),
        sa.Column("span_id", sa.String(16), nullable=False, unique=True),
        sa.Column("parent_span_id", sa.String(16), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="internal"),
        sa.Column("status", sa.String(32), nullable=False, server_default="ok"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attributes", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    )
    op.create_index("ix_spans_run_id", "spans", ["run_id"])
    op.create_index("ix_spans_trace_id", "spans", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_spans_trace_id", table_name="spans")
    op.drop_index("ix_spans_run_id", table_name="spans")
    op.drop_table("spans")
