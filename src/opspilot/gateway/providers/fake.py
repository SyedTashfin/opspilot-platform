"""Deterministic provider used by tests, CI and dry runs.

Design rules:

* Same request in, same response out. No clock, no randomness, no network.
* Structured requests produce a *valid* instance of the requested Pydantic model, so schema
  plumbing is exercised without a model.
* Failure injection is explicit and scripted, so retry and fallback logic can be tested precisely
  instead of by mocking internals.
* Token accounting is an approximation and says so: a real tokenizer is the provider's business, and
  pretending to be one here would make evaluation numbers look more precise than they are.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from opspilot.gateway.errors import ProviderBadResponse, ProviderError
from opspilot.gateway.types import ModelRequest, ProviderResult, Usage

FAKE_MODEL = "fake/fake-1"
MAX_STRUCTURED_DEPTH = 4


def approximate_tokens(text: str) -> int:
    """Rough token count (chars / 4, minimum 1). Documented as an approximation, not a tokenizer."""
    return max(1, len(text) // 4)


def _scalar_for(annotation: Any, name: str, depth: int) -> Any:
    origin = get_origin(annotation)
    if origin is Literal:
        # A Literal is part of the schema: pick its first declared option so the generated payload
        # validates. Without this, a Literal field makes every structured fake response invalid.
        options = get_args(annotation)
        return options[0] if options else None
    if origin in (Union, UnionType):
        options = get_args(annotation)
        # An optional field resolves to None: the neutral value, and predictable for tests that
        # assert
        # on structured output without a model in the loop.
        if type(None) in options:
            return None
        return _scalar_for(options[0], name, depth) if options else None
    if origin in (list, tuple, set, frozenset):
        args = get_args(annotation)
        return [_scalar_for(args[0], name, depth) if args else f"fake {name}"]
    if origin is dict:
        return {}
    if isinstance(annotation, type):
        if annotation is str:
            return f"fake {name}"
        if annotation is bool:
            return True
        if annotation is int:
            return 1
        if annotation is float:
            return 0.1
        if issubclass(annotation, Enum):
            return next(iter(annotation)).value
        if issubclass(annotation, BaseModel) and depth < MAX_STRUCTURED_DEPTH:
            return _build(annotation, depth + 1).model_dump(mode="json")
    return f"fake {name}"


def _build(model: type[BaseModel], depth: int = 0) -> BaseModel:
    payload = {
        name: _scalar_for(field_info.annotation, name, depth)
        for name, field_info in model.model_fields.items()
    }
    return model.model_validate(payload)


@dataclass
class FakeProvider:
    """Scriptable deterministic provider.

    ``fail_first``: raise ``failure`` for the first N calls to each model, then succeed.
    ``fail_always``: always raise, to drive the fallback path to exhaustion.
    ``stuck``: sleep past the gateway timeout for the first N calls, to drive timeout handling.
    """

    name: str = "fake"
    fail_first: int = 0
    fail_always: bool = False
    failure: ProviderError = field(default_factory=lambda: ProviderError("scripted provider failure"))
    timeout_first: int = 0
    timeout_seconds: float = 5.0
    calls: list[tuple[str, str]] = field(default_factory=list)

    def supports(self, model: str) -> bool:
        return model.startswith("fake")

    def _maybe_fail(self, model: str, attempt_index: int) -> None:
        if self.fail_always:
            raise self.failure
        if attempt_index < self.fail_first:
            raise self.failure

    async def complete(self, model: str, request: ModelRequest) -> ProviderResult:
        attempt_index = sum(1 for called_model, _ in self.calls if called_model == model)
        self.calls.append((model, request.step))
        if attempt_index < self.timeout_first:
            import asyncio

            await asyncio.sleep(self.timeout_seconds)
        self._maybe_fail(model, attempt_index)

        prompt_tokens = sum(approximate_tokens(message.content) for message in request.messages)
        if request.response_model is not None:
            parsed = _build(request.response_model)
            text = json.dumps(parsed.model_dump(mode="json"), sort_keys=True)
            if parsed is None:  # pragma: no cover - defensive, _build always returns an instance
                raise ProviderBadResponse("fake provider failed to build a structured response")
        else:
            parsed = None
            text = f"fake completion for step {request.step}"

        return ProviderResult(
            text=text,
            parsed=parsed,
            usage=Usage(input_tokens=prompt_tokens, output_tokens=approximate_tokens(text)),
            model=model,
            provider=self.name,
            request_id=f"fake-{len(self.calls)}",
        )
