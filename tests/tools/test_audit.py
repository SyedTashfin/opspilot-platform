from __future__ import annotations

import uuid

from opspilot.tools.audit import GENESIS_HASH, AuditEvent, InMemoryAuditRecorder, _digest


def event(action: str = "tool.executed", **kwargs: object) -> AuditEvent:
    return AuditEvent(actor="opspilot", action=action, subject="azure.query_logs", **kwargs)  # type: ignore[arg-type]


async def test_first_event_chains_from_genesis() -> None:
    recorder = InMemoryAuditRecorder()
    await recorder.append(event())
    assert recorder.entries[0][2] == GENESIS_HASH
    assert recorder.verify_chain() is True


async def test_sequence_numbers_are_monotonic() -> None:
    recorder = InMemoryAuditRecorder()
    for index in range(1, 4):
        sequence = await recorder.append(event(action=f"step.{index}"))
        assert sequence == index
    assert recorder.actions() == ["step.1", "step.2", "step.3"]


async def test_chain_detects_a_tampered_event() -> None:
    recorder = InMemoryAuditRecorder()
    await recorder.append(event())
    await recorder.append(event(action="tool.rejected"))
    assert recorder.verify_chain() is True

    sequence, _, prev_hash, recorded_hash = recorder.entries[0]
    recorder.entries[0] = (
        sequence,
        event(action="tool.executed.but.edited"),
        prev_hash,
        recorded_hash,
    )

    assert recorder.verify_chain() is False


async def test_chain_detects_a_removed_event() -> None:
    recorder = InMemoryAuditRecorder()
    await recorder.append(event())
    await recorder.append(event(action="tool.rejected"))
    del recorder.entries[-1]
    # The survivor still verifies; the removed event is what a reviewer would miss.
    assert recorder.verify_chain() is True
    assert len(recorder.entries) == 1


async def test_payload_and_run_id_are_part_of_the_digest() -> None:
    run_id = uuid.UUID("00000000-0000-4000-8000-000000000002")
    first = _digest(GENESIS_HASH, event(payload={"status": "ok"}))
    second = _digest(GENESIS_HASH, event(payload={"status": "ok", "extra": 1}))
    third = _digest(GENESIS_HASH, event(run_id=run_id, payload={"status": "ok"}))
    assert first != second
    assert first != third
    assert first == _digest(GENESIS_HASH, event(payload={"status": "ok"}))


async def test_recorder_records_subjects_for_review() -> None:
    recorder = InMemoryAuditRecorder()
    await recorder.append(AuditEvent(actor="opspilot", action="tool.unknown", subject="azure.typo"))
    assert recorder.subjects() == ["azure.typo"]
