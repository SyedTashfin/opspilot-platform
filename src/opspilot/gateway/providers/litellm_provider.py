"""Live provider backed by LiteLLM.

LiteLLM is used strictly as a transport adapter: it knows how to talk to each vendor and how to
normalise usage reporting, and that is all we ask of it. Model policy, retries, fallback, budget
enforcement and cost accounting stay in the gateway, so replacing this file with hand-written vendor
clients later would not change a single caller.

The import is lazy and lives inside ``complete``: the library is heavy, and every unit test — plus
every CI run — goes through the deterministic fake provider instead.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from opspilot.gateway.errors import (
    ProviderBadResponse,
    ProviderError,
    ProviderRequestRejected,
    ProviderTimeout,
)
from opspilot.gateway.types import ModelRequest, ProviderResult, Usage

DEFAULT_PREFIXES = ("deepseek/", "mistral/", "openai/", "azure/", "openrouter/", "anthropic/")

# Non-retryable litellm exception class names, mapped by name so this module still imports cleanly
# when litellm is not installed.
_REJECTION_NAMES = (
    "AuthenticationError",
    "BadRequestError",
    "PermissionDeniedError",
    "NotFoundError",
    "UnprocessableEntityError",
    "ContextWindowExceededError",
)
_TIMEOUT_NAMES = ("Timeout", "APITimeoutError")


def _schema_instruction(response_model: type[BaseModel]) -> str:
    schema = json.dumps(response_model.model_json_schema(), sort_keys=True)
    return (
        "Respond with a single JSON object and nothing else. It must validate against this JSON "
        f"schema: {schema}"
    )


def _parse_structured(response_model: type[BaseModel], text: str) -> BaseModel:
    try:
        return response_model.model_validate_json(text)
    except ValidationError as exc:
        raise ProviderBadResponse(
            f"response did not validate against {response_model.__name__}: "
            f"{exc.error_count()} validation errors"
        ) from exc
    except ValueError as exc:
        raise ProviderBadResponse(f"response was not JSON: {exc}") from exc


class LiteLLMProvider:
    """Adapter over ``litellm.acompletion``."""

    name = "litellm"

    def __init__(self, prefixes: tuple[str, ...] = DEFAULT_PREFIXES) -> None:
        self._prefixes = prefixes

    def supports(self, model: str) -> bool:
        return model.startswith(self._prefixes)

    async def complete(self, model: str, request: ModelRequest) -> ProviderResult:
        try:
            import litellm
        except ImportError as exc:  # pragma: no cover - dependency is declared; guard is for clarity
            raise ProviderError("litellm is not installed; live providers are unavailable") from exc

        messages: list[dict[str, str]] = [
            {"role": message.role, "content": message.content} for message in request.messages
        ]
        if request.response_model is not None:
            messages.append({"role": "system", "content": _schema_instruction(request.response_model)})

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": request.temperature,
            "timeout": request.metadata.get("timeout_seconds"),
        }
        if request.max_output_tokens is not None:
            kwargs["max_tokens"] = request.max_output_tokens

        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:
            raise _translate(exc) from exc

        text, usage = _extract(response)
        parsed = (
            _parse_structured(request.response_model, text) if request.response_model is not None else None
        )
        return ProviderResult(
            text=text,
            parsed=parsed,
            usage=usage,
            model=str(getattr(response, "model", model) or model),
            provider=self.name,
            request_id=str(getattr(response, "id", "") or "") or None,
        )


def _extract(response: Any) -> tuple[str, Usage]:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError) as exc:
        raise ProviderBadResponse("provider response had no choices") from exc
    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    if prompt_tokens == 0 and completion_tokens == 0:
        raise ProviderBadResponse("provider reported no usage; cost would be unverifiable")
    return str(content or ""), Usage(input_tokens=prompt_tokens, output_tokens=completion_tokens)


def _translate(exc: Exception) -> ProviderError:
    name = type(exc).__name__
    message = str(exc)[:400]
    if name in _TIMEOUT_NAMES:
        return ProviderTimeout(f"provider timed out: {message}")
    if name in _REJECTION_NAMES:
        return ProviderRequestRejected(f"provider rejected the request ({name}): {message}")
    return ProviderError(f"provider call failed ({name}): {message}")
