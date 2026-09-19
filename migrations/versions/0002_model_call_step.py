"""model_calls.step: attribute each model call to the pipeline step that issued it

Revision ID: 0002_model_call_step
Revises: 0001_initial
Create Date: 2026-09-19

Without this column the database could answer "what did this run cost?" but not "which step cost
that?", which makes cost attribution a guess. The recorder already carried the step name; it was
being dropped on the way to storage. Nullable-free with a server default so the column exists for
any pre-existing row and every future insert has a value.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_model_call_step"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

UNSPECIFIED = "unspecified"


def upgrade() -> None:
    op.add_column(
        "model_calls",
        sa.Column(
            "step",
            sa.String(64),
            nullable=False,
            server_default=sa.text(f"'{UNSPECIFIED}'"),
        ),
    )
    op.create_index("ix_model_calls_step", "model_calls", ["step"])


def downgrade() -> None:
    op.drop_index("ix_model_calls_step", table_name="model_calls")
    op.drop_column("model_calls", "step")
