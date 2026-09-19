"""Structured JSON logging (one line per event, machine-parseable)."""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    """Idempotent: safe to call from the app lifespan and from tests."""
    global _CONFIGURED
    level_name = level.upper()
    level_value: int = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level_value)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_value),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    if not _CONFIGURED:
        configure_logging()
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
