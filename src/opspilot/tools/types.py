"""Tool definitions and invocation types.

A tool declares what it accepts, what it returns, who may call it and what it costs in risk. The
platform derives authorisation from that declaration rather than from a convention, and the same
declaration is what an LLM sees when tools are offered to it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from opspilot.domain.enums import CallStatus, PermissionClass, RiskLevel


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Who is calling, on whose behalf. ``agent`` is the audit actor for everything a run does."""

    agent: str
    run_id: uuid.UUID | None = None


class ToolHandler:  # pragma: no cover - protocol holder, implemented as a plain callable
    """Marker for documentation; handlers are plain async callables."""


Handler = Callable[[Any, ToolContext], Awaitable[BaseModel | None]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    handler: Handler
    input_model: type[BaseModel]
    output_model: type[BaseModel] | None = None
    permission_class: PermissionClass = PermissionClass.READ_ONLY
    risk_level: RiskLevel = RiskLevel.LOW
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.name or "." not in self.name:
            msg = f"tool name {self.name!r} must be namespaced, for example 'azure.query_logs'"
            raise ValueError(msg)
        if self.timeout_seconds <= 0:
            msg = f"tool {self.name!r} must declare a positive timeout"
            raise ValueError(msg)

    @property
    def requires_approval(self) -> bool:
        """Restricted mutations need a human; ADMIN tools are not agent-callable at all."""
        return self.permission_class is PermissionClass.WRITE_RESTRICTED

    @property
    def agent_callable(self) -> bool:
        return self.permission_class is not PermissionClass.ADMIN

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def output_schema(self) -> dict[str, Any] | None:
        return self.output_model.model_json_schema() if self.output_model is not None else None

    def as_llm_tool(self) -> dict[str, Any]:
        """The shape an LLM tool-calling API expects, plus our governance metadata."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema(),
            "opspilot": {
                "permission_class": self.permission_class.value,
                "risk_level": self.risk_level.value,
                "requires_approval": self.requires_approval,
            },
        }


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """The result of one attempt, successful or refused. Every outcome is audited."""

    tool_name: str
    status: CallStatus
    permission_class: PermissionClass
    risk_level: RiskLevel
    arguments: Mapping[str, Any] = field(default_factory=dict)
    arguments_hash: str = ""
    output: BaseModel | None = None
    latency_ms: int = 0
    error: str | None = None

    @property
    def executed(self) -> bool:
        return self.status is CallStatus.OK

    @property
    def summary(self) -> str:
        if self.status is CallStatus.OK:
            return f"{self.tool_name} succeeded in {self.latency_ms}ms"
        return f"{self.tool_name} {self.status.value}: {self.error or 'no detail'}"


def canonical_arguments(arguments: Mapping[str, Any]) -> str:
    """Stable representation used for the approval binding hash."""
    import json

    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)


def arguments_hash(arguments: Mapping[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(canonical_arguments(arguments).encode("utf-8")).hexdigest()


def tool_names(definitions: Sequence[ToolDefinition]) -> tuple[str, ...]:
    return tuple(definition.name for definition in definitions)
