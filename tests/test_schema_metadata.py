"""Schema assertions that catch drift without needing a running PostgreSQL."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

import opspilot.db.models  # noqa: F401  (registers the mappings)
from opspilot.db.base import Base

EXPECTED_TABLES = {
    "agents",
    "runs",
    "run_steps",
    "model_calls",
    "tool_calls",
    "approvals",
    "audit_events",
}


def test_expected_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_schema_compiles_as_postgres_ddl(table_name: str) -> None:
    ddl = str(
        sa.schema.CreateTable(Base.metadata.tables[table_name]).compile(
            dialect=postgresql.dialect()
        )
    )
    assert ddl.strip().startswith("CREATE TABLE")
    assert table_name in ddl


def test_json_columns_become_jsonb_on_postgres() -> None:
    ddl = str(
        sa.schema.CreateTable(Base.metadata.tables["agents"]).compile(dialect=postgresql.dialect())
    )
    assert "JSONB" in ddl


def test_status_columns_are_varchar_without_native_enum() -> None:
    status_type = Base.metadata.tables["agents"].c.status.type
    assert isinstance(status_type, sa.Enum)
    assert status_type.native_enum is False
    assert status_type.length == 32


def test_money_and_tokens_are_numeric_and_integer() -> None:
    runs = Base.metadata.tables["runs"]
    assert isinstance(runs.c.cost_eur.type, sa.Numeric)
    assert isinstance(runs.c.input_tokens.type, sa.Integer)
    assert isinstance(runs.c.trace_id.type, sa.String)


def test_audit_events_are_append_only_ready() -> None:
    audit = Base.metadata.tables["audit_events"]
    assert {"seq", "prev_hash", "hash", "actor", "action", "payload"} <= set(audit.c.keys())


def test_every_foreign_key_has_an_explicit_name() -> None:
    for table in Base.metadata.sorted_tables:
        for constraint in table.constraints:
            if isinstance(constraint, sa.ForeignKeyConstraint):
                assert constraint.name is not None
