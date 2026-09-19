"""Runtime configuration.

Every setting is environment-overridable with the ``OPSPILOT_`` prefix, so nothing in the codebase
hardcodes an environment, a credential or a budget.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="OPSPILOT_", extra="ignore", frozen=True
    )

    environment: Literal["local", "ci", "dev", "prod-demo"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    service_name: str = "opspilot-api"
    api_host: str = "0.0.0.0"  # noqa: S104 - binds all interfaces inside the container only
    api_port: int = Field(default=8000, ge=1, le=65535)

    database_url: str = "postgresql+asyncpg://opspilot:opspilot@localhost:5432/opspilot"

    # Model gateway policy (enforced from M2 onward).
    default_model: str = "deepseek/deepseek-chat"
    fallback_model: str = "mistral/mistral-small-latest"
    run_cost_cap_eur: float = Field(default=0.25, gt=0, le=5.0)
    daily_cost_cap_eur: float = Field(default=2.0, gt=0, le=50.0)

    # Run safety limits: every run has deterministic ceilings (never an unbounded loop).
    run_max_steps: int = Field(default=40, ge=1, le=200)
    run_timeout_seconds: int = Field(default=900, ge=30, le=7200)
    tool_timeout_seconds: int = Field(default=60, ge=1, le=600)

    # Human approval for restricted tools.
    approval_ttl_seconds: int = Field(default=900, ge=60, le=86400)

    otel_exporter_otlp_endpoint: str | None = None

    @model_validator(mode="after")
    def _check_budget_ordering(self) -> Settings:
        if self.run_cost_cap_eur > self.daily_cost_cap_eur:
            msg = "run_cost_cap_eur must not exceed daily_cost_cap_eur"
            raise ValueError(msg)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
