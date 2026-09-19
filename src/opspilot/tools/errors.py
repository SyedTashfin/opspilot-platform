"""Tool-layer failures.

Refusals that are part of normal operation (permission denied, approval missing, invalid arguments)
are *outcomes*, not exceptions: they are returned as a ``ToolOutcome`` and audited. Exceptions are
reserved for wiring bugs an operator must fix.
"""

from __future__ import annotations


class ToolError(Exception):
    """Base class for tool-layer failures."""


class ToolNotFound(ToolError):
    """The requested tool is not registered. A wiring bug, not an operational refusal."""


class ToolAlreadyRegistered(ToolError):
    """Two tools claimed the same name."""


class ApprovalDenied(ToolError):
    """An approval token was supplied but is invalid, expired, already used, or bound elsewhere."""
