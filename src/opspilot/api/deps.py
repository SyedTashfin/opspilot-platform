"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.config import Settings, get_settings
from opspilot.db.session import get_session

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
