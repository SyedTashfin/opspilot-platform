"""The tool registry: the single place that knows which tools exist and what they may do.

The registry is deliberately declarative. Governance decisions (approval, timeouts, refusals)
live in the executor, so a tool can never grant itself more authority than its declaration.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from opspilot.tools.errors import ToolAlreadyRegistered, ToolNotFound
from opspilot.tools.types import ToolDefinition


class ToolRegistry:
    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ToolAlreadyRegistered(f"tool {definition.name!r} is already registered")
        self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFound(f"tool {name!r} is not registered") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def for_agent(self, allowed_tools: Sequence[str] | None) -> tuple[ToolDefinition, ...]:
        """The tools an agent may see.

        ``allowed_tools=None`` means "everything the registry holds except ADMIN tools"; an explicit
        list is intersected with the registry and never widened by a typo — an unknown name is an
        error the caller must fix, not a silently ignored entry.
        """
        if allowed_tools is None:
            return tuple(sorted(self._tools.values(), key=lambda d: d.name))
        missing = [name for name in allowed_tools if name not in self._tools]
        if missing:
            msg = f"agent requests unregistered tools: {', '.join(sorted(missing))}"
            raise ToolNotFound(msg)
        return tuple(self._tools[name] for name in sorted(allowed_tools))

    def llm_tools(self, allowed_tools: Sequence[str] | None = None) -> list[dict[str, Any]]:
        return [
            definition.as_llm_tool()
            for definition in self.for_agent(allowed_tools)
            if definition.agent_callable
        ]

    def governance_snapshot(self) -> list[dict[str, Any]]:
        """What the Tools page renders: name, permission class, risk, timeout, approval."""
        return [
            {
                "name": definition.name,
                "description": definition.description,
                "permission_class": definition.permission_class.value,
                "risk_level": definition.risk_level.value,
                "requires_approval": definition.requires_approval,
                "agent_callable": definition.agent_callable,
                "timeout_seconds": definition.timeout_seconds,
                "input_schema": definition.input_schema(),
            }
            for definition in sorted(self._tools.values(), key=lambda d: d.name)
        ]
