"""Gateway behaviour: retries, fallback, timeouts, budget refusal, accounting, schema invariants.

Every provider here is a stub or the deterministic fake, so the suite never sleeps, never calls a
model and never costs anything.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from opspilot.domain.enums import CallStatus
from opspilot.gateway.accounting import InMemoryLedger, InMemoryRecorder
from opspilot.gateway.errors import (
    AllModelsFailed,
    BudgetExceeded,
    ProviderBadResponse,
    ProviderError,
    ProviderTimeout,
)
from opspilot.gateway.gateway import GatewayConfig, ModelGateway
from opspilot.gateway.policy import ModelPolicy
from opspilot.gateway.pricing import cost_eur
from opspilot.gateway.providers.fake import FakeProvider
from opspilot.gateway.types import Message, ModelRequest, ProviderResult, Usage

RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
PRIMARY = "deepseek/deepseek-chat"
FALLBACK = "mistral/mistral-small-latest"


class Detail(BaseModel):
    summary: str
    confidence: float


@dataclass
class StubProvider:
    """Minimal provider with scripted behaviour; satisfies the ModelProvider protocol."""

    name: str
    models: tuple[str, ...]
    behaviour: str = "ok"  # ok | error | timeout | bad
    fail_times: int = 0
    calls: list[str] = field(default_factory=list)

    def supports(self, model: str) -> bool:
        return model in self.models

    async def complete(self, model: str, request: ModelRequest) -> ProviderResult:
        self.calls.append(model)
        if len(self.calls) <= self.fail_times:
            raise ProviderError("scripted transient failure")
        if self.behaviour == "error":
            raise ProviderError("scripted failure")
        if self.behaviour == "timeout":
            raise ProviderTimeout("scripted timeout")
        if self.behaviour == "bad":
            raise ProviderBadResponse("scripted bad response")
        return ProviderResult(
            text="stub output",
            usage=Usage(input_tokens=100, output_tokens=20),
            model=model,
            provider=self.name,
        )


@dataclass
class Harness:
    gateway: ModelGateway
    recorder: InMemoryRecorder
    ledger: InMemoryLedger
    sleeps: list[float]


def harness(
    providers: list[StubProvider | FakeProvider],
    *,
    primary: str = PRIMARY,
    fallback: str = FALLBACK,
    run_cap: float = 0.25,
    daily_cap: float = 2.0,
    max_attempts: int = 3,
    backoff_base: float = 0.5,
    backoff_max: float = 4.0,
    ledger: InMemoryLedger | None = None,
) -> Harness:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    recorder = InMemoryRecorder()
    resolved_ledger = ledger or InMemoryLedger()
    gateway = ModelGateway(
        providers=providers,
        recorder=recorder,
        ledger=resolved_ledger,
        config=GatewayConfig(
            policy=ModelPolicy.from_settings(primary, fallback),
            max_attempts=max_attempts,
            timeout_seconds=5.0,
            backoff_base_seconds=backoff_base,
            backoff_max_seconds=backoff_max,
            run_cost_cap_eur=run_cap,
            daily_cost_cap_eur=daily_cap,
        ),
        sleep=fake_sleep,
    )
    return Harness(gateway=gateway, recorder=recorder, ledger=resolved_ledger, sleeps=sleeps)


def request(step: str = "diagnose", response_model: type[BaseModel] | None = None) -> ModelRequest:
    return ModelRequest(
        step=step,
        messages=(Message.user("investigate the incident"),),
        response_model=response_model,
    )


async def test_a_priced_call_credits_the_ledger() -> None:
    """Without a credit path the in-memory ledger stays at zero and every cost ceiling is decorative."""
    stub = StubProvider(name="stub", models=(PRIMARY,))
    h = harness([stub])

    assert await h.ledger.spent_in_run_eur(RUN_ID) == 0.0

    response = await h.gateway.complete(request(), RUN_ID)

    assert response.cost_known is True
    assert await h.ledger.spent_in_run_eur(RUN_ID) == pytest.approx(response.cost_eur or 0.0)
    assert await h.ledger.spent_today_eur() == pytest.approx(response.cost_eur or 0.0)
    assert h.ledger.credited == 1


async def test_an_unpriced_call_credits_nothing_rather_than_zero() -> None:
    stub = StubProvider(name="stub", models=("exotic/model-1",))
    h = harness([stub], primary="exotic/model-1")

    response = await h.gateway.complete(request(), RUN_ID)

    assert response.cost_known is False
    assert await h.ledger.spent_in_run_eur(RUN_ID) == 0.0
    assert h.ledger.credited == 0


async def test_success_records_one_call_with_known_cost() -> None:
    stub = StubProvider(name="stub", models=(PRIMARY,))
    h = harness([stub])

    response = await h.gateway.complete(request(), RUN_ID)

    assert response.model == PRIMARY
    assert response.attempts == 1
    assert response.fallback_used is False
    assert response.cost_known is True
    # Derived from the price table rather than restating a rate here: a price change must be one edit.
    expected, _ = cost_eur(response.model, response.usage, at=datetime.now(UTC))
    assert response.cost_eur == pytest.approx(expected or 0.0)
    assert len(h.recorder.calls) == 1
    assert h.recorder.calls[0].status is CallStatus.OK
    assert h.sleeps == []


async def test_retry_then_success_uses_exponential_backoff() -> None:
    stub = StubProvider(name="stub", models=(PRIMARY,), fail_times=2)
    h = harness([stub], max_attempts=3)

    response = await h.gateway.complete(request(), RUN_ID)

    assert response.attempts == 3
    assert len(stub.calls) == 3
    assert h.sleeps == [0.5, 1.0]
    assert len(h.recorder.calls) == 1  # only the successful call is recorded as spend


async def test_backoff_is_capped() -> None:
    stub = StubProvider(name="stub", models=(PRIMARY,), fail_times=4)
    h = harness([stub], max_attempts=5, backoff_base=1.0, backoff_max=2.0)

    await h.gateway.complete(request(), RUN_ID)

    assert h.sleeps == [1.0, 2.0, 2.0, 2.0]


async def test_exhausted_retries_move_to_the_fallback_model() -> None:
    primary = StubProvider(name="primary", models=(PRIMARY,), behaviour="timeout")
    fallback = StubProvider(name="fallback", models=(FALLBACK,))
    h = harness([primary, fallback], max_attempts=3)

    response = await h.gateway.complete(request(), RUN_ID)

    assert response.model == FALLBACK
    assert response.fallback_used is True
    assert response.attempts == 1  # first attempt on the fallback model
    assert len(primary.calls) == 3  # three attempts on the primary, no more


async def test_non_retryable_failure_does_not_retry_the_same_model() -> None:
    primary = StubProvider(name="primary", models=(PRIMARY,), behaviour="bad")
    fallback = StubProvider(name="fallback", models=(FALLBACK,))
    h = harness([primary, fallback], max_attempts=3)

    response = await h.gateway.complete(request(), RUN_ID)

    assert response.model == FALLBACK
    assert len(primary.calls) == 1
    assert h.sleeps == []  # no backoff, because retrying was pointless


async def test_every_model_failing_raises_and_records_one_error_row() -> None:
    primary = StubProvider(name="primary", models=(PRIMARY,), behaviour="error")
    fallback = StubProvider(name="fallback", models=(FALLBACK,), behaviour="error")
    h = harness([primary, fallback])

    with pytest.raises(AllModelsFailed) as excinfo:
        await h.gateway.complete(request(), RUN_ID)

    assert PRIMARY in str(excinfo.value)
    assert FALLBACK in str(excinfo.value)
    assert len(h.recorder.calls) == 1
    error_row = h.recorder.calls[0]
    assert error_row.status is CallStatus.ERROR
    assert error_row.model == FALLBACK
    assert error_row.error is not None


async def test_run_budget_is_enforced_before_any_call() -> None:
    stub = StubProvider(name="stub", models=(PRIMARY,))
    ledger = InMemoryLedger(by_run={RUN_ID: 0.25})
    h = harness([stub], run_cap=0.25, ledger=ledger)

    with pytest.raises(BudgetExceeded) as excinfo:
        await h.gateway.complete(request(), RUN_ID)

    assert excinfo.value.scope == "run"
    assert stub.calls == []
    assert h.recorder.calls == []


async def test_daily_budget_is_enforced_before_any_call() -> None:
    stub = StubProvider(name="stub", models=(PRIMARY,))
    ledger = InMemoryLedger(today=2.0)
    h = harness([stub], daily_cap=2.0, ledger=ledger)

    with pytest.raises(BudgetExceeded) as excinfo:
        await h.gateway.complete(request(), RUN_ID)

    assert excinfo.value.scope == "daily"
    assert stub.calls == []


async def test_unpriced_model_reports_unknown_cost_instead_of_zero() -> None:
    stub = StubProvider(name="stub", models=("exotic/model",))
    h = harness([stub], primary="exotic/model", fallback="exotic/model")

    response = await h.gateway.complete(request(), RUN_ID)

    assert response.cost_known is False
    assert response.cost_eur is None
    assert h.recorder.calls[0].cost_known is False
    assert h.recorder.calls[0].cost_eur == 0.0


async def test_model_without_a_provider_fails_with_a_clear_reason() -> None:
    h = harness([StubProvider(name="stub", models=(PRIMARY,))], primary="ghost/model")

    with pytest.raises(AllModelsFailed, match="no provider registered"):
        await h.gateway.complete(request(), RUN_ID)


async def test_structured_request_is_parsed_by_the_provider() -> None:
    provider = FakeProvider()
    h = harness([provider], primary="fake/fake-1", fallback="fake/fake-1")

    response = await h.gateway.complete(request(response_model=Detail), RUN_ID)

    assert isinstance(response.parsed, Detail)
    assert response.cost_known is True
    assert response.cost_eur == 0.0


async def test_missing_structured_object_falls_back_to_a_provider_that_produces_one() -> None:
    # A provider that returns prose where a schema was demanded is a real failure mode; it must not
    # be accepted, and it must not be retried on the same model.
    prose_only = StubProvider(name="prose", models=(PRIMARY,))
    structured = FakeProvider()
    h = harness([prose_only, structured], fallback="fake/fake-1")

    response = await h.gateway.complete(request(response_model=Detail), RUN_ID)

    assert isinstance(response.parsed, Detail)
    assert response.model == "fake" or response.model.startswith("fake")
    assert response.fallback_used is True
    assert len(prose_only.calls) == 1
