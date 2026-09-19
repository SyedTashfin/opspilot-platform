"""Test environment: no live model, no live database, no network."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("OPSPILOT_ENVIRONMENT", "ci")
os.environ.setdefault("OPSPILOT_LOG_LEVEL", "WARNING")


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Settings are cached per process; clear the cache around each test."""
    from opspilot.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
