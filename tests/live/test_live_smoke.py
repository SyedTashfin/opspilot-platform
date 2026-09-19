"""Live provider smoke test: opt-in, cost-printing, never part of a default run.

    uv run pytest -m live -s

Requires a provider key in the environment or in ``.env`` (for example ``DEEPSEEK_API_KEY``).
Without one it skips, so no ordinary test run and no CI job can ever spend money.
"""

from __future__ import annotations

import os
import uuid

import pytest
from pydantic import BaseModel

from opspilot.config import Settings
from opspilot.gateway.accounting import InMemoryLedger, InMemoryRecorder
from opspilot.gateway.gateway import gateway_from_settings
from opspilot.gateway.providers.litellm_provider import LiteLLMProvider
from opspilot.gateway.types import Message, ModelRequest

pytestmark = pytest.mark.live

KEYS = ("DEEPSEEK_API_KEY", "MISTRAL_API_KEY", "OPENAI_API_KEY")


class Classification(BaseModel):
    """Small structured object: exercises the schema path against a real provider."""

    category: str
    confidence: float


def _has_key() -> bool:
    return any(os.environ.get(name) for name in KEYS)


@pytest.mark.skipif(not _has_key(), reason="no live provider key configured")
async def test_one_live_call_reports_usage_and_cost() -> None:
    settings = Settings(environment="dev")
    recorder = InMemoryRecorder()
    gateway = gateway_from_settings(
        settings,
        providers=[LiteLLMProvider()],
        recorder=recorder,
        ledger=InMemoryLedger(),
    )

    response = await gateway.complete(
        ModelRequest(
            step="live-smoke",
            messages=(
                Message.system("You are terse."),
                Message.user(
                    "Classify this alert as database, network or deployment: 'connection pool exhausted'."
                ),
            ),
            response_model=Classification,
        ),
        run_id=uuid.uuid4(),
    )

    print(
        "\n[LIVE] "
        f"provider={response.provider} model={response.model} "
        f"tokens={response.usage.total_tokens} cost_eur={response.cost_eur} "
        f"cost_known={response.cost_known} latency_ms={response.latency_ms} "
        f"attempts={response.attempts}"
    )

    assert response.usage.total_tokens > 0
    assert response.text.strip() != ""
    assert response.cost_known is True, "live cost must be computable from the price table"
    assert len(recorder.calls) == 1
