"""Append-only audit trail with a tamper-evident hash chain.

Every audit row carries the hash of the previous row, so a deleted or edited event breaks the chain
and is detectable (`verify()` in the in-memory recorder; a job in M8 for the database). The chain is
computed over the event content only — no timestamps — so verification is reproducible.

Honest limitation, stated rather than hidden: the Postgres recorder reads the previous hash and then
inserts, which is not safe against concurrent writers. At this scale (one writer per run, demo
traffic) that is acceptable; making it serialisable is a transaction-level concern for M8, where the
audit trail acquires a reviewed security posture.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.db.models import AuditEvent as AuditEventRow

GENESIS_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class AuditEvent:
    actor: str
    action: str
    subject: str | None = None
    run_id: uuid.UUID | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


def _digest(prev_hash: str, event: AuditEvent) -> str:
    body = json.dumps(
        {
            "actor": event.actor,
            "action": event.action,
            "subject": event.subject,
            "run_id": str(event.run_id) if event.run_id else None,
            "payload": event.payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(f"{prev_hash}{body}".encode()).hexdigest()


class AuditRecorder(Protocol):
    async def append(self, event: AuditEvent) -> int:
        """Persist one event and return its sequence number."""
        ...


@dataclass
class InMemoryAuditRecorder:
    """Test double that can also verify its own chain."""

    entries: list[tuple[int, AuditEvent, str, str]] = field(default_factory=list)

    async def append(self, event: AuditEvent) -> int:
        sequence = len(self.entries) + 1
        prev_hash = self.entries[-1][3] if self.entries else GENESIS_HASH
        self.entries.append((sequence, event, prev_hash, _digest(prev_hash, event)))
        return sequence

    def actions(self) -> list[str]:
        return [event.action for _, event, _, _ in self.entries]

    def subjects(self) -> list[str | None]:
        return [event.subject for _, event, _, _ in self.entries]

    def verify_chain(self) -> bool:
        prev = GENESIS_HASH
        for _, event, recorded_prev, recorded_hash in self.entries:
            if recorded_prev != prev:
                return False
            if _digest(prev, event) != recorded_hash:
                return False
            prev = recorded_hash
        return True


class PostgresAuditRecorder:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: AuditEvent) -> int:
        last = await self._session.execute(
            sa.select(AuditEventRow.hash).order_by(AuditEventRow.seq.desc()).limit(1)
        )
        prev_hash = last.scalar_one_or_none() or GENESIS_HASH
        row = AuditEventRow(
            actor=event.actor,
            action=event.action,
            subject=event.subject,
            run_id=event.run_id,
            payload=dict(event.payload),
            prev_hash=prev_hash,
            hash=_digest(prev_hash, event),
        )
        self._session.add(row)
        await self._session.flush()
        return int(row.seq)
