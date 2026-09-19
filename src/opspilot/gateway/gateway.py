"""The gateway itself: policy, retries, timeouts, budget, accounting, traceability.

Sequence for one logical call:

1. Refuse early if the run or daily cost ceiling is already reached (``BudgetExceeded``): a run must
   not start spending it cannot afford.
2. Resolve the model chain for the requested step from the policy.
3. For each model: find a provider, attempt up to ``max_attempts`` with bounded exponential backoff,
   retrying only failures that can plausibly succeed on a retry.
4. On success: compute cost, record the call, return a ``ModelResponse`` that carries its own
   accounting facts.
5. On total failure: record one error row and raise ``AllModelsFailed`` with every reason attached.

The gateway never sleeps in tests: the sleep function is injected.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from opspilot.domain.enums import CallStatus
from opspilot.gateway.accounting import CostLedger, ModelCallRecord, ModelCallRecorder
from opspilot.gateway.errors import (
    AllModelsFailed,
    BudgetExceeded,
    GatewayError,
    ProviderBadResponse,
    ProviderError,
    ProviderRequestRejected,
    ProviderTimeout,
)
from opspilot.gateway.policy import ModelPolicy
from opspilot.gateway.pricing import cost_eur
from opspilot.gateway.providers.base import ModelProvider
from opspilot.gateway.types import ModelRequest, ModelResponse, ProviderResult
from opspilot.observability.logging import get_logger
from opspilot.observability.tracing import record_error, set_attributes, span

logger = get_logger(__name__)

NON_RETRYABLE = (ProviderBadResponse, ProviderRequestRejected)


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    policy: ModelPolicy
    max_attempts: int = 3
    timeout_seconds: float = 60.0
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    run_cost_cap_eur: float = 0.25
    daily_cost_cap_eur: float = 2.0


@dataclass
class ModelGateway:
    providers: Sequence[ModelProvider]
    recorder: ModelCallRecorder
    ledger: CostLedger
    config: GatewayConfig
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)
    clock: Callable[[], float] = field(default=time.perf_counter)

    async def complete(self, request: ModelRequest, run_id: uuid.UUID) -> ModelResponse:
        """Public entry point: one span per model request, carrying the outcome as attributes.

        The span records what the platform did — step, model, provider, tokens, cost, attempts, whether
        a fallback was used, latency — and never the prompt or the completion.
        """
        with span("gateway.complete", {"llm.step": request.step, "run.id": str(run_id)}) as call_span:
            try:
                response = await self._complete(request, run_id)
            except Exception as exc:
                record_error(call_span, str(exc), type(exc).__name__)
                raise
            # cost_eur is None when the model has no price on file. Record the cost only when it is
            # known: writing 0.0 would put a plausible zero in the trace, which is the one thing the
            # pricing table exists to avoid.
            # Credit the ledger so the ceilings have something to enforce against. Without this the
            # in-memory ledger stays at zero, every cost budget is decorative, and a run reports itself
            # as free — a bug that a free deterministic provider can hide indefinitely.
            if response.cost_known and response.cost_eur:
                await self.ledger.credit(run_id, float(response.cost_eur))
            cost_attributes = (
                {"llm.cost_eur": float(response.cost_eur)}
                if response.cost_known and response.cost_eur is not None
                else {}
            )
            set_attributes(
                call_span,
                {
                    "llm.model": response.model,
                    "llm.provider": response.provider,
                    "llm.request_id": response.request_id,
                    "llm.input_tokens": response.usage.input_tokens,
                    "llm.output_tokens": response.usage.output_tokens,
                    "llm.cost_known": response.cost_known,
                    **cost_attributes,
                    "llm.attempts": response.attempts,
                    "llm.fallback_used": response.fallback_used,
                    "llm.latency_ms": response.latency_ms,
                },
            )
            return response

    async def _complete(self, request: ModelRequest, run_id: uuid.UUID) -> ModelResponse:
        await self._enforce_budget(run_id)
        chain = self.config.policy.chain_for(request.step)
        request_id = uuid.uuid4().hex
        reasons: list[str] = []

        for index, model in enumerate(chain.models):
            provider = self._provider_for(model)
            if provider is None:
                reasons.append(f"{model}: no provider registered for this model")
                continue
            logger.info(
                "gateway.call.start",
                step=request.step,
                model=model,
                attempt_index=index,
                run_id=str(run_id),
                request_id=request_id,
            )
            try:
                return await self._call_with_retries(
                    provider=provider,
                    model=model,
                    request=request,
                    run_id=run_id,
                    request_id=request_id,
                    fallback_used=index > 0,
                )
            except NON_RETRYABLE as exc:
                reasons.append(f"{model}: {exc}")
            except GatewayError as exc:
                reasons.append(f"{model}: {exc}")

        await self._record_failure(run_id, request, reasons, chain.models)
        raise AllModelsFailed(
            f"step {request.step!r} failed for every model in the chain: " + "; ".join(reasons)
        )

    def _provider_for(self, model: str) -> ModelProvider | None:
        for provider in self.providers:
            if provider.supports(model):
                return provider
        return None

    async def _enforce_budget(self, run_id: uuid.UUID) -> None:
        spent_today = await self.ledger.spent_today_eur()
        if spent_today >= self.config.daily_cost_cap_eur:
            raise BudgetExceeded("daily", self.config.daily_cost_cap_eur, spent_today)
        spent_in_run = await self.ledger.spent_in_run_eur(run_id)
        if spent_in_run >= self.config.run_cost_cap_eur:
            raise BudgetExceeded("run", self.config.run_cost_cap_eur, spent_in_run)

    def _backoff_seconds(self, attempt: int) -> float:
        base: float = self.config.backoff_base_seconds
        cap: float = self.config.backoff_max_seconds
        delay: float = base * float(2 ** (attempt - 1))
        return min(delay, cap)

    async def _call_with_retries(
        self,
        *,
        provider: ModelProvider,
        model: str,
        request: ModelRequest,
        run_id: uuid.UUID,
        request_id: str,
        fallback_used: bool,
    ) -> ModelResponse:
        last_error: GatewayError | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            started = self.clock()
            result = None
            try:
                result = await asyncio.wait_for(
                    provider.complete(model, request), timeout=self.config.timeout_seconds
                )
            except TimeoutError:
                last_error = ProviderTimeout(f"{model} exceeded the {self.config.timeout_seconds}s timeout")
            except NON_RETRYABLE:
                raise
            except GatewayError as exc:
                last_error = exc
            except Exception as exc:  # a provider bug must not kill the run
                last_error = ProviderError(f"unexpected provider error: {type(exc).__name__}")

            if result is not None:
                latency_ms = int((self.clock() - started) * 1000)
                response = self._build_response(
                    result=result,
                    request=request,
                    request_id=request_id,
                    latency_ms=latency_ms,
                    attempts=attempt,
                    fallback_used=fallback_used,
                )
                await self.record_success(response, run_id, request)
                return response

            if attempt < self.config.max_attempts:
                delay = self._backoff_seconds(attempt)
                logger.warning(
                    "gateway.call.retry",
                    model=model,
                    attempt=attempt,
                    delay_seconds=delay,
                    error=str(last_error),
                )
                await self.sleep(delay)

        if last_error is None:  # pragma: no cover - unreachable: the loop runs at least once
            last_error = ProviderError(f"{model} produced no result and no error")
        raise last_error

    def _build_response(
        self,
        *,
        result: ProviderResult,
        request: ModelRequest,
        request_id: str,
        latency_ms: int,
        attempts: int,
        fallback_used: bool,
    ) -> ModelResponse:
        parsed = result.parsed
        if request.response_model is not None and parsed is None:
            raise ProviderBadResponse(
                f"provider {result.provider} returned no structured object for "
                f"{request.response_model.__name__}"
            )
        amount, known = cost_eur(result.model, result.usage)
        return ModelResponse(
            text=result.text,
            parsed=parsed,
            usage=result.usage,
            model=result.model,
            provider=result.provider,
            request_id=result.request_id or request_id,
            latency_ms=latency_ms,
            attempts=attempts,
            fallback_used=fallback_used,
            cost_eur=amount,
            cost_known=known,
            step=request.step,
        )

    async def record_success(self, response: ModelResponse, run_id: uuid.UUID, request: ModelRequest) -> None:
        await self.recorder.record(ModelCallRecord.from_response(run_id, response, request.step))
        logger.info(
            "gateway.call.finish",
            step=request.step,
            model=response.model,
            provider=response.provider,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=response.latency_ms,
            cost_eur=response.cost_eur,
            cost_known=response.cost_known,
            attempts=response.attempts,
            fallback_used=response.fallback_used,
        )

    async def _record_failure(
        self,
        run_id: uuid.UUID,
        request: ModelRequest,
        reasons: Sequence[str],
        models: Sequence[str],
    ) -> None:
        await self.recorder.record(
            ModelCallRecord(
                run_id=run_id,
                step=request.step,
                provider="none",
                model=models[-1] if models else "unknown",
                request_id=uuid.uuid4().hex,
                input_tokens=0,
                output_tokens=0,
                cost_eur=0.0,
                cost_known=True,
                latency_ms=0,
                status=CallStatus.ERROR,
                attempts=self.config.max_attempts,
                fallback_used=len(models) > 1,
                error="; ".join(reasons)[:900],
            )
        )


def gateway_from_settings(
    settings: Any,
    providers: Sequence[ModelProvider],
    recorder: ModelCallRecorder,
    ledger: CostLedger,
    *,
    model: str | None = None,
) -> ModelGateway:
    """Build a gateway from application settings.

    Kept here rather than in ``config`` so configuration stays free of provider dependencies.

    ``model`` overrides the primary model in the chain. It exists because a provider answers only for the
    models it claims: asking the deterministic provider for the configured DeepSeek model is a wiring
    error that surfaces as "no provider registered for this model" at the first call, which is a
    confusing way to learn that the wrong model name was requested.
    """
    policy = ModelPolicy.from_settings(
        default_model=model or settings.default_model,
        fallback_model=settings.fallback_model,
        overrides=settings.model_policy_overrides,
        extra_fallbacks=settings.extra_fallbacks,
    )
    return ModelGateway(
        providers=providers,
        recorder=recorder,
        ledger=ledger,
        config=GatewayConfig(
            policy=policy,
            max_attempts=settings.model_max_attempts,
            timeout_seconds=settings.model_timeout_seconds,
            backoff_base_seconds=settings.model_backoff_base_seconds,
            backoff_max_seconds=settings.model_backoff_max_seconds,
            run_cost_cap_eur=settings.run_cost_cap_eur,
            daily_cost_cap_eur=settings.daily_cost_cap_eur,
        ),
    )
