from __future__ import annotations

import uuid

from opspilot.domain.enums import CallStatus
from opspilot.gateway.accounting import (
    InMemoryLedger,
    InMemoryRecorder,
    ModelCallRecord,
)
from opspilot.gateway.types import ModelResponse, Usage


def _response(*, cost: float | None, known: bool) -> ModelResponse:
    return ModelResponse(
        text="ok",
        parsed=None,
        usage=Usage(input_tokens=1_000, output_tokens=500),
        model="deepseek/deepseek-chat",
        provider="litellm",
        request_id="req-1",
        latency_ms=120,
        attempts=2,
        fallback_used=True,
        cost_eur=cost,
        cost_known=known,
        step="diagnose",
    )


def test_record_maps_a_response_onto_the_model_calls_row() -> None:
    run_id = uuid.uuid4()
    record = ModelCallRecord.from_response(run_id, _response(cost=0.00075, known=True))
    assert record.run_id == run_id
    assert record.step == "diagnose"
    assert record.input_tokens == 1_000
    assert record.output_tokens == 500
    assert record.cost_eur == 0.00075
    assert record.cost_known is True
    assert record.attempts == 2
    assert record.fallback_used is True
    assert record.status is CallStatus.OK


def test_unknown_cost_is_stored_as_zero_but_flagged() -> None:
    record = ModelCallRecord.from_response(uuid.uuid4(), _response(cost=None, known=False))
    assert record.cost_eur == 0.0
    assert record.cost_known is False


async def test_recorder_totals_and_surfaces_unknown_cost_calls() -> None:
    recorder = InMemoryRecorder()
    await recorder.record(
        ModelCallRecord.from_response(uuid.uuid4(), _response(cost=0.5, known=True))
    )
    await recorder.record(
        ModelCallRecord.from_response(uuid.uuid4(), _response(cost=None, known=False))
    )
    assert recorder.total_eur() == 0.5
    assert len(recorder.unknown_cost_calls()) == 1


async def test_ledger_reports_run_and_daily_spend_independently() -> None:
    run_id = uuid.uuid4()
    ledger = InMemoryLedger(by_run={run_id: 0.2}, today=1.4)
    assert await ledger.spent_in_run_eur(run_id) == 0.2
    assert await ledger.spent_in_run_eur(uuid.uuid4()) == 0.0
    assert await ledger.spent_today_eur() == 1.4
