"""Gateway value types.

Deliberately framework-free: the runtime, the API and the evaluation harness all speak these types,
so swapping the underlying provider library never leaks into callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str

    @staticmethod
    def system(content: str) -> Message:
        return Message(role="system", content=content)

    @staticmethod
    def user(content: str) -> Message:
        return Message(role="user", content=content)

    @staticmethod
    def assistant(content: str) -> Message:
        return Message(role="assistant", content=content)


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One logical model call, expressed in platform terms.

    ``step`` is what the model policy resolves against (for example ``diagnose`` or ``classify``),
    so routing is configuration rather than a hardcoded model name at the call site.
    """

    step: str
    messages: tuple[Message, ...]
    response_model: type[BaseModel] | None = None
    temperature: float = 0.0
    max_output_tokens: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """What a provider returns before accounting is applied."""

    text: str
    usage: Usage
    model: str
    provider: str
    request_id: str | None = None
    parsed: BaseModel | None = None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """What callers receive, including the accounting facts they must not have to recompute."""

    text: str
    parsed: BaseModel | None
    usage: Usage
    model: str
    provider: str
    request_id: str
    latency_ms: int
    attempts: int
    fallback_used: bool
    cost_eur: float | None
    cost_known: bool
    step: str
