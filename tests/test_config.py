from __future__ import annotations

import pytest
from pydantic import ValidationError

from opspilot.config import Settings


def test_defaults_are_safe() -> None:
    settings = Settings(environment="ci")
    assert settings.run_max_steps <= 200
    assert settings.run_timeout_seconds >= 30
    assert settings.tool_timeout_seconds >= 1
    assert settings.approval_ttl_seconds >= 60
    assert settings.run_cost_cap_eur <= settings.daily_cost_cap_eur
    assert settings.fallback_model != ""


def test_run_budget_cannot_exceed_daily_budget() -> None:
    with pytest.raises(ValidationError, match="run_cost_cap_eur must not exceed"):
        Settings(run_cost_cap_eur=5.0, daily_cost_cap_eur=1.0)


@pytest.mark.parametrize(
    ("field", "value"),
    [("run_max_steps", 10_000), ("run_timeout_seconds", 1), ("approval_ttl_seconds", 5)],
)
def test_run_limits_are_bounded(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_settings_are_immutable() -> None:
    settings = Settings(environment="ci")
    with pytest.raises(ValidationError):
        settings.environment = "local"  # type: ignore[misc]
