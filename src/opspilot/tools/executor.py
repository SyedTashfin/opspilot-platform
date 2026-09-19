"""The tool executor: the only place a tool is invoked.

Order of operations, every time:

1. Resolve the tool. An unknown name is a wiring bug: audited, then raised.
2. Validate arguments against the tool's input schema. Invalid arguments never reach the handler.
3. Apply the permission gate:
   * ``READ_ONLY`` and ``WRITE_SAFE`` run directly.
   * ``WRITE_RESTRICTED`` requires a valid, single-use approval bound to *these exact arguments*;
     without one the outcome is ``WAITING_APPROVAL`` and the handler is not called.
   * ``ADMIN`` is refused outright — no agent reaches it, with or without an approval.
4. Run the handler under the tool's own timeout.
5. Validate the output against the declared output schema, if one is declared.
6. Audit the outcome — success, refusal, timeout or error. Nothing is silent.

The executor never raises for an operational refusal; it returns a ``ToolOutcome`` so the caller can
decide whether to retry, escalate for approval, or abandon the step.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from opspilot.domain.enums import CallStatus, PermissionClass
from opspilot.observability.logging import get_logger
from opspilot.observability.tracing import record_error, set_attributes, span
from opspilot.tools.audit import AuditEvent, AuditRecorder
from opspilot.tools.registry import ToolRegistry
from opspilot.tools.types import ToolContext, ToolDefinition, ToolOutcome, arguments_hash

logger = get_logger(__name__)


class ApprovalLookup(Protocol):
    async def consume(self, *, token: str, tool_name: str, arguments_hash: str) -> bool:
        """Validate *and* consume a single-use approval.

        Must return True only for a valid, unexpired, unused approval whose bound arguments hash
        matches, and must make a second call with the same token fail.
        """
        ...


class DenyAllApprovals:
    """Default: nothing is pre-approved. Replaced by the real lookup in M8."""

    async def consume(self, *, token: str, tool_name: str, arguments_hash: str) -> bool:
        return False


@dataclass
class ToolExecutor:
    registry: ToolRegistry
    audit: AuditRecorder
    approvals: ApprovalLookup | None = None
    clock: Callable[[], float] = time.perf_counter

    async def execute(
        self,
        name: str,
        raw_arguments: Mapping[str, Any],
        context: ToolContext,
        *,
        approval_token: str | None = None,
    ) -> ToolOutcome:
        try:
            definition = self.registry.get(name)
        except Exception:
            await self._audit(
                context,
                action="tool.unknown",
                subject=name,
                payload={"arguments": dict(raw_arguments), "requested_tool": name},
            )
            raise

        digest = arguments_hash(raw_arguments)

        parsed, error = self._validate_arguments(definition.input_model, raw_arguments)
        if parsed is None:
            return await self._refuse(
                context, name, CallStatus.ERROR, raw_arguments, digest, error or "invalid arguments"
            )

        gate = await self._permission_gate(name, digest, context, approval_token)
        if gate is not None:
            return await self._refuse(
                context,
                name,
                gate[0],
                raw_arguments,
                digest,
                gate[1],
                permission=definition.permission_class,
            )

        started = self.clock()
        with span(
            "tool.execute",
            {
                "tool.name": name,
                "tool.permission_class": definition.permission_class.value,
                "tool.risk_level": definition.risk_level.value,
                "tool.arguments_hash": digest,
                "run.id": str(context.run_id) if context.run_id else "",
            },
        ) as tool_span:
            output, failure = await self._invoke(definition, parsed, context)
            latency_ms = int((self.clock() - started) * 1000)
            # Set on the span before it closes: a finished span ignores further attributes.
            set_attributes(
                tool_span,
                {
                    "tool.status": failure[0].value if failure is not None else CallStatus.OK.value,
                    "tool.latency_ms": latency_ms,
                },
            )
            if failure is not None:
                record_error(tool_span, failure[1], failure[0].value)

        if failure is not None:
            return await self._refuse(context, name, failure[0], raw_arguments, digest, failure[1])
        output_error = self._validate_output(definition.output_model, output)
        status = CallStatus.OK if output_error is None else CallStatus.ERROR

        outcome = ToolOutcome(
            tool_name=name,
            status=status,
            permission_class=definition.permission_class,
            risk_level=definition.risk_level,
            arguments=dict(raw_arguments),
            arguments_hash=digest,
            output=output if status is CallStatus.OK else None,
            latency_ms=latency_ms,
            error=output_error,
        )
        await self._audit(
            context,
            action="tool.executed" if status is CallStatus.OK else "tool.output_invalid",
            subject=name,
            payload={
                "status": status.value,
                "permission_class": definition.permission_class.value,
                "risk_level": definition.risk_level.value,
                "arguments_hash": digest,
                "latency_ms": latency_ms,
                "output_schema_version": getattr(definition.output_model, "__name__", None),
                "error": output_error,
            },
        )
        logger.info(
            "tool.call",
            tool=name,
            status=status.value,
            latency_ms=latency_ms,
            run_id=str(context.run_id) if context.run_id else None,
        )
        return outcome

    async def _invoke(
        self, definition: ToolDefinition, parsed: BaseModel, context: ToolContext
    ) -> tuple[BaseModel | None, tuple[CallStatus, str] | None]:
        """Run the handler under its own timeout, returning either an output or a refusal."""
        try:
            output = await asyncio.wait_for(
                definition.handler(parsed, context), timeout=definition.timeout_seconds
            )
        except TimeoutError:
            return None, (
                CallStatus.TIMEOUT,
                f"exceeded the {definition.timeout_seconds}s timeout",
            )
        except Exception as exc:
            logger.warning("tool.handler_failed", tool=definition.name, error=str(exc))
            return None, (CallStatus.ERROR, f"handler raised {type(exc).__name__}: {exc}")
        return output, None

    async def _permission_gate(
        self,
        name: str,
        digest: str,
        context: ToolContext,
        approval_token: str | None,
    ) -> tuple[CallStatus, str] | None:
        """Return a refusal status and reason, or None to proceed."""
        definition = self.registry.get(name)
        if definition.permission_class is PermissionClass.ADMIN:
            return (
                CallStatus.REJECTED,
                "admin tools are not callable by agents under any circumstances",
            )
        if not definition.requires_approval:
            return None
        lookup = self.approvals
        if approval_token is None or lookup is None:
            return (
                CallStatus.WAITING_APPROVAL,
                f"{name} is write_restricted and requires human approval bound to these arguments",
            )
        approved = await lookup.consume(token=approval_token, tool_name=name, arguments_hash=digest)
        if not approved:
            return (
                CallStatus.REJECTED,
                f"approval {approval_token!r} is invalid, expired, already used, or bound to "
                f"different arguments",
            )
        await self._audit(
            context,
            action="approval.consumed",
            subject=name,
            payload={"arguments_hash": digest},
        )
        return None

    def _validate_arguments(
        self, model: type[BaseModel], raw: Mapping[str, Any]
    ) -> tuple[BaseModel | None, str | None]:
        try:
            return model.model_validate(dict(raw)), None
        except ValidationError as exc:
            return None, f"arguments do not match {model.__name__}: {exc.error_count()} errors"

    def _validate_output(self, model: type[BaseModel] | None, output: BaseModel | None) -> str | None:
        if model is None:
            return None
        if output is None:
            return f"handler returned no output but {model.__name__} is declared"
        try:
            model.model_validate(output.model_dump())
        except ValidationError as exc:
            return f"output does not match {model.__name__}: {exc.error_count()} errors"
        return None

    async def _refuse(
        self,
        context: ToolContext,
        name: str,
        status: CallStatus,
        raw_arguments: Mapping[str, Any],
        digest: str,
        reason: str,
        permission: PermissionClass = PermissionClass.READ_ONLY,
    ) -> ToolOutcome:
        await self._audit(
            context,
            action=f"tool.{status.value}",
            subject=name,
            payload={
                "status": status.value,
                "reason": reason,
                "arguments_hash": digest,
                "arguments": dict(raw_arguments),
            },
        )
        logger.warning("tool.refused", tool=name, status=status.value, reason=reason)
        return ToolOutcome(
            tool_name=name,
            status=status,
            permission_class=permission,
            risk_level=self._risk_for(name),
            arguments=dict(raw_arguments),
            arguments_hash=digest,
            error=reason,
        )

    def _risk_for(self, name: str) -> Any:
        try:
            return self.registry.get(name).risk_level
        except Exception:  # pragma: no cover - the tool resolved earlier in the same call
            from opspilot.domain.enums import RiskLevel

            return RiskLevel.LOW

    async def _audit(
        self,
        context: ToolContext,
        *,
        action: str,
        subject: str | None,
        payload: Mapping[str, Any],
    ) -> None:
        await self.audit.append(
            AuditEvent(
                actor=context.agent,
                action=action,
                subject=subject,
                run_id=context.run_id,
                payload=dict(payload),
            )
        )
