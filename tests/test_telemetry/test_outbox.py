from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from opensquilla.telemetry.consent import TelemetryScope
from opensquilla.telemetry.contracts import (
    TELEMETRY_EVENT_ADAPTER,
    TelemetryWireTarget,
    canonical_json_bytes,
    parse_telemetry_wire,
    telemetry_protocol_manifest,
)
from opensquilla.telemetry.contracts.common import StrictTelemetryModel
from opensquilla.telemetry.outbox import (
    EnqueueResult,
    OutboxEventConflictError,
    OutboxLimits,
    OutboxPriority,
    TelemetryOutbox,
)


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


@dataclass
class FakeClock:
    now_ms: int = 1_788_224_400_000

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, milliseconds: int) -> None:
        self.now_ms += milliseconds


def _turn_event(number: int = 1, *, duration_ms: int = 120, notice: str = "notice-v1"):
    payload = {
        "event_name": "turn_result",
        "event_version": 1,
        "event_id": _uuid(number),
        "occurred_at_utc": "2026-09-01T01:02:03.456Z",
        "source": "gateway",
        "app_version": "1.2.3",
        "platform": "linux",
        "outcome": "success",
        "error_code": None,
        "duration_ms": duration_ms,
        "consent_scope": "reliability",
        "notice_version": notice,
        "sample_rate": 1.0,
        "app_session_id": _uuid(900),
        "ttft_ms": min(40, duration_ms),
        "stall_count": 0,
        "stall_threshold_ms": 15_000,
    }
    return TELEMETRY_EVENT_ADAPTER.validate_json(json.dumps(payload), strict=True)


def _growth_event(number: int = 1):
    payload = {
        "event_name": "first_app_ready",
        "event_version": 1,
        "event_id": _uuid(number),
        "occurred_at_utc": "2026-09-01T01:02:03.456Z",
        "source": "desktop",
        "app_version": "1.2.3",
        "platform": "windows",
        "outcome": None,
        "error_code": None,
        "duration_ms": None,
        "consent_scope": "growth",
        "notice_version": "growth-v2",
        "sample_rate": 1,
        "analytics_user_id": _uuid(901),
    }
    return TELEMETRY_EVENT_ADAPTER.validate_json(json.dumps(payload), strict=True)


async def _open(
    root: Path,
    scope: TelemetryScope,
    *,
    clock: FakeClock | None = None,
    limits: OutboxLimits | None = None,
) -> TelemetryOutbox:
    return await TelemetryOutbox.open(root, scope, clock=clock, limits=limits)


async def test_scopes_use_distinct_wal_databases_and_busy_timeout(tmp_path: Path) -> None:
    reliability = await _open(tmp_path, TelemetryScope.RELIABILITY)
    growth = await _open(tmp_path, TelemetryScope.GROWTH)
    try:
        assert reliability.database_path != growth.database_path
        assert reliability.database_path.name == "reliability-outbox.sqlite3"
        assert growth.database_path.name == "growth-outbox.sqlite3"
        assert reliability.database_path.parent == tmp_path / "telemetry"

        with sqlite3.connect(reliability.database_path) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000

        if os.name != "nt":
            assert reliability.database_path.stat().st_mode & 0o077 == 0
            assert reliability.database_path.parent.stat().st_mode & 0o077 == 0
    finally:
        await reliability.close()
        await growth.close()


async def test_enqueue_persists_only_revalidated_canonical_scope_payload(tmp_path: Path) -> None:
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY)
    event = _turn_event()
    try:
        assert await outbox.enqueue(event) is EnqueueResult.ENQUEUED

        batch = await outbox.claim_batch()
        assert batch is not None
        assert batch.events[0].payload == canonical_json_bytes(event)
        assert json.loads(batch.events[0].payload)["event_name"] == "turn_result"

        with pytest.raises(TypeError):
            await outbox.enqueue(event.model_dump(mode="json"))  # type: ignore[arg-type]

        invalid = event.model_copy(update={"duration_ms": -1})
        with pytest.raises(ValidationError):
            await outbox.enqueue(invalid)
    finally:
        await outbox.close()


async def test_enqueue_rejects_event_from_other_scope(tmp_path: Path) -> None:
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        with pytest.raises(ValueError, match="scope"):
            await outbox.enqueue(_growth_event())
    finally:
        await outbox.close()


async def test_duplicate_is_idempotent_but_changed_payload_conflicts(tmp_path: Path) -> None:
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        assert await outbox.enqueue(_turn_event(1, duration_ms=120)) is EnqueueResult.ENQUEUED
        assert await outbox.enqueue(_turn_event(1, duration_ms=120)) is EnqueueResult.DUPLICATE

        with pytest.raises(OutboxEventConflictError):
            await outbox.enqueue(_turn_event(1, duration_ms=121))
    finally:
        await outbox.close()


async def test_claim_lease_recovers_after_expiry_and_counts_attempts(tmp_path: Path) -> None:
    clock = FakeClock()
    limits = OutboxLimits(lease_ms=1_000)
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY, clock=clock, limits=limits)
    try:
        await outbox.enqueue(_turn_event())

        first = await outbox.claim_batch()
        assert first is not None
        assert first.events[0].attempt_count == 1
        assert await outbox.claim_batch() is None

        clock.advance(999)
        assert await outbox.claim_batch() is None
        clock.advance(1)

        recovered = await outbox.claim_batch()
        assert recovered is not None
        assert recovered.lease_id != first.lease_id
        assert recovered.events[0].event_id == first.events[0].event_id
        assert recovered.events[0].attempt_count == 2
    finally:
        await outbox.close()


async def test_expired_lease_late_ack_and_retry_cannot_touch_reclaimed_row(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    outbox = await _open(
        tmp_path,
        TelemetryScope.RELIABILITY,
        clock=clock,
        limits=OutboxLimits(lease_ms=1_000),
    )
    try:
        await outbox.enqueue(_turn_event())
        expired = await outbox.claim_batch()
        assert expired is not None

        clock.advance(1_000)
        current = await outbox.claim_batch()
        assert current is not None
        assert current.lease_id != expired.lease_id

        assert (
            await outbox.release_for_retry(
                expired.lease_id,
                next_attempt_at_ms=clock.now_ms + 5_000,
            )
            == 0
        )
        assert await outbox.acknowledge(expired.lease_id) == 0
        stats = await outbox.stats()
        assert stats.pending_events == 1
        assert stats.leased_events == 1
        assert await outbox.acknowledge(current.lease_id) == 1
    finally:
        await outbox.close()


async def test_acknowledge_deletes_only_rows_owned_by_lease(tmp_path: Path) -> None:
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        await outbox.enqueue(_turn_event())
        batch = await outbox.claim_batch()
        assert batch is not None

        assert await outbox.acknowledge("unknown-lease") == 0
        assert (await outbox.stats()).pending_events == 1
        assert await outbox.acknowledge(batch.lease_id) == 1
        assert (await outbox.stats()).pending_events == 0
    finally:
        await outbox.close()


async def test_retry_release_defers_reclaim_until_available_time(tmp_path: Path) -> None:
    clock = FakeClock()
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY, clock=clock)
    try:
        await outbox.enqueue(_turn_event())
        batch = await outbox.claim_batch()
        assert batch is not None

        await outbox.release_for_retry(batch.lease_id, next_attempt_at_ms=clock.now_ms + 5_000)
        assert await outbox.claim_batch() is None
        clock.advance(5_000)
        assert await outbox.claim_batch() is not None
    finally:
        await outbox.close()


async def test_unattempted_release_restores_attempt_counter(tmp_path: Path) -> None:
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        await outbox.enqueue(_turn_event())
        first = await outbox.claim_batch()
        assert first is not None
        await outbox.release_unattempted(first.lease_id)

        second = await outbox.claim_batch()
        assert second is not None
        assert second.events[0].attempt_count == 1
    finally:
        await outbox.close()


async def test_discard_claimed_events_deletes_only_selected_and_releases_peers(
    tmp_path: Path,
) -> None:
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        await outbox.enqueue(_turn_event(1))
        await outbox.enqueue(_turn_event(2))
        batch = await outbox.claim_batch()
        assert batch is not None

        assert await outbox.discard_claimed_events(batch.lease_id, (_uuid(1),)) == 1
        stats = await outbox.stats()
        assert stats.pending_events == 1
        assert stats.leased_events == 0

        reclaimed = await outbox.claim_batch()
        assert reclaimed is not None
        assert [event.event_id for event in reclaimed.events] == [_uuid(2)]
        assert reclaimed.events[0].attempt_count == 1
    finally:
        await outbox.close()


async def test_discard_claimed_events_requires_explicit_nonempty_ids(
    tmp_path: Path,
) -> None:
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        with pytest.raises(ValueError, match="event_ids"):
            await outbox.discard_claimed_events("lease", ())
        with pytest.raises(ValueError, match="event_ids"):
            await outbox.discard_claimed_events("lease", ("",))
    finally:
        await outbox.close()


async def test_capacity_evicts_low_priority_oldest_before_high_priority(tmp_path: Path) -> None:
    clock = FakeClock()
    limits = OutboxLimits(max_events=2, max_payload_bytes=1_000_000)
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY, clock=clock, limits=limits)
    try:
        await outbox.enqueue(_turn_event(1), priority=OutboxPriority.LOW)
        clock.advance(1)
        await outbox.enqueue(_turn_event(2), priority=OutboxPriority.HIGH)
        clock.advance(1)
        assert (
            await outbox.enqueue(_turn_event(3), priority=OutboxPriority.NORMAL)
            is EnqueueResult.ENQUEUED
        )

        batch = await outbox.claim_batch()
        assert batch is not None
        assert {event.event_id for event in batch.events} == {_uuid(2), _uuid(3)}
    finally:
        await outbox.close()


async def test_byte_capacity_evicts_until_under_limit(tmp_path: Path) -> None:
    payload_size = len(canonical_json_bytes(_turn_event(1)))
    limits = OutboxLimits(max_events=10, max_payload_bytes=payload_size * 2)
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY, limits=limits)
    try:
        for number in range(1, 4):
            await outbox.enqueue(_turn_event(number), priority=OutboxPriority.NORMAL)

        stats = await outbox.stats()
        assert stats.pending_events == 2
        assert stats.payload_bytes <= limits.max_payload_bytes
    finally:
        await outbox.close()


async def test_ttl_removes_expired_rows_including_stale_leases(tmp_path: Path) -> None:
    clock = FakeClock()
    limits = OutboxLimits(ttl_ms=1_000, lease_ms=10_000)
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY, clock=clock, limits=limits)
    try:
        await outbox.enqueue(_turn_event())
        assert await outbox.claim_batch() is not None
        clock.advance(1_000)

        assert await outbox.claim_batch() is None
        assert (await outbox.stats()).pending_events == 0
    finally:
        await outbox.close()


async def test_quarantine_keeps_metadata_but_never_payload(tmp_path: Path) -> None:
    clock = FakeClock()
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY, clock=clock)
    try:
        await outbox.enqueue(_turn_event())
        batch = await outbox.claim_batch()
        assert batch is not None

        assert await outbox.quarantine(batch.lease_id, status_code=422, reason="contract") == 1
        stats = await outbox.stats()
        assert stats.pending_events == 0
        assert stats.rejected_events == 1

        rejected = await outbox.list_rejections()
        assert rejected[0].event_id == _uuid(1)
        assert rejected[0].event_name == "turn_result"
        assert rejected[0].status_code == 422

        with sqlite3.connect(outbox.database_path) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(telemetry_rejections)")
            }
        assert "payload" not in columns
        assert "payload_json" not in columns
    finally:
        await outbox.close()


async def test_rejection_metadata_is_bounded_by_count_and_ttl(tmp_path: Path) -> None:
    clock = FakeClock()
    limits = OutboxLimits(rejection_max_events=2, rejection_ttl_ms=1_000)
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY, clock=clock, limits=limits)
    try:
        for number in range(1, 4):
            await outbox.enqueue(_turn_event(number))
            batch = await outbox.claim_batch()
            assert batch is not None
            assert await outbox.quarantine(
                batch.lease_id,
                status_code=422,
                reason="contract",
            ) == 1
            clock.advance(1)

        rejected = await outbox.list_rejections()
        assert [event.event_id for event in rejected] == [_uuid(2), _uuid(3)]

        clock.advance(1_000)
        assert (await outbox.stats()).rejected_events == 0
        assert await outbox.list_rejections() == ()
    finally:
        await outbox.close()


def _device_activity(number: int = 1):
    payload = _growth_event(number).model_dump(mode="json")
    payload.update(event_name="product_active", source="gateway", surface="cli", device_id="a" * 64)
    return TELEMETRY_EVENT_ADAPTER.validate_json(json.dumps(payload), strict=True)


async def _reject_activity(outbox: TelemetryOutbox, event, *, status: int = 422) -> None:
    await outbox.enqueue(event)
    batch = await outbox.claim_batch()
    assert batch is not None
    await outbox.quarantine(
        batch.lease_id, status_code=status, reason="contract" if status == 422 else "conflict",
    )


async def test_contract_recovery_upgrades_old_database_and_preserves_original_expiry(
    tmp_path: Path,
) -> None:
    event = _device_activity()
    occurred = int(event.occurred_at_utc.timestamp() * 1_000)
    clock = FakeClock(occurred + 86_400_000)
    outbox = await _open(tmp_path, TelemetryScope.GROWTH, clock=clock)
    await _reject_activity(outbox, event)
    database_path = outbox.database_path
    await outbox.close()
    with sqlite3.connect(database_path) as connection:
        # Exact pre-recovery layout, including the original rejection metadata.
        connection.execute("DROP TABLE telemetry_contract_recoveries")
        before = connection.execute("SELECT * FROM telemetry_rejections").fetchall()
    outbox = await _open(tmp_path, TelemetryScope.GROWTH, clock=clock)
    try:
        with sqlite3.connect(database_path) as connection:
            assert connection.execute("SELECT * FROM telemetry_rejections").fetchall() == before
        assert await outbox.recover_contract_rejection_once(event)
        batch = await outbox.claim_batch()
        assert batch is not None and batch.events[0].payload == canonical_json_bytes(event)
        with sqlite3.connect(database_path) as connection:
            assert connection.execute(
                "SELECT created_at_ms, expires_at_ms FROM telemetry_outbox"
            ).fetchone() == (occurred, occurred + 30 * 86_400_000)
        await outbox.acknowledge(batch.lease_id)
        assert await outbox.list_rejections() == ()
        assert not await outbox.recover_contract_rejection_once(event)
        # Even a second rejection cannot turn the retained ledger into an endless retry source.
        await _reject_activity(outbox, event)
    finally:
        await outbox.close()
    outbox = await _open(tmp_path, TelemetryScope.GROWTH, clock=clock)
    try:
        assert not await outbox.recover_contract_rejection_once(event)
        assert (await outbox.stats()).pending_events == 0
        await outbox.clear_scope()
        with sqlite3.connect(database_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM telemetry_contract_recoveries"
            ).fetchone() == (0,)
    finally:
        await outbox.close()


@pytest.mark.parametrize(
    "case", ["expired", "future", "no_device", "missing", "conflict", "wrong_name"],
)
async def test_contract_recovery_requires_recent_matching_device_rejection(tmp_path, case):
    event = _device_activity()
    occurred = int(event.occurred_at_utc.timestamp() * 1_000)
    clock = FakeClock(occurred + 1000)
    outbox = await _open(tmp_path, TelemetryScope.GROWTH, clock=clock)
    try:
        if case != "missing":
            await _reject_activity(outbox, event, status=409 if case == "conflict" else 422)
        if case == "expired":
            clock.now_ms = occurred + 30 * 86_400_000
        elif case == "future":
            clock.now_ms = occurred - 1
        elif case == "no_device":
            event = event.model_copy(update={"device_id": None})
        elif case == "wrong_name":
            with sqlite3.connect(outbox.database_path) as connection:
                connection.execute("UPDATE telemetry_rejections SET event_name = 'client_launch'")
        assert not await outbox.recover_contract_rejection_once(event)
        assert (await outbox.stats()).pending_events == 0
    finally:
        await outbox.close()


async def test_recovery_capacity_preserves_pending_and_never_evicts_attempt_markers(tmp_path):
    event = _device_activity()
    occurred = int(event.occurred_at_utc.timestamp() * 1000)
    clock = FakeClock(occurred + 1000)
    outbox = await _open(
        tmp_path, TelemetryScope.GROWTH, clock=clock,
        limits=OutboxLimits(max_events=1, rejection_max_events=1),
    )
    try:
        await _reject_activity(outbox, event)
        await outbox.enqueue(_device_activity(2))
        assert not await outbox.recover_contract_rejection_once(event)
        pending = await outbox.claim_batch()
        assert pending is not None and pending.events[0].event_id == _uuid(2)
        await outbox.acknowledge(pending.lease_id)
        assert await outbox.recover_contract_rejection_once(event)
        recovered = await outbox.claim_batch()
        assert recovered is not None
        await outbox.acknowledge(recovered.lease_id)
        await _reject_activity(outbox, _device_activity(3))
        assert not await outbox.recover_contract_rejection_once(_device_activity(3))
        with sqlite3.connect(outbox.database_path) as connection:
            assert connection.execute(
                "SELECT event_id FROM telemetry_contract_recoveries"
            ).fetchall() == [(_uuid(1),)]
        clock.now_ms = occurred + 30 * 86_400_000
        assert not await outbox.recover_contract_rejection_once(event)
        await outbox.stats()
        with sqlite3.connect(outbox.database_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM telemetry_contract_recoveries"
            ).fetchone() == (0,)
    finally:
        await outbox.close()


async def test_recovery_payload_and_attempt_marker_commit_together(tmp_path, monkeypatch):
    event = _device_activity()
    clock = FakeClock(int(event.occurred_at_utc.timestamp() * 1000) + 1000)
    outbox = await _open(tmp_path, TelemetryScope.GROWTH, clock=clock)
    try:
        await _reject_activity(outbox, event)
        execute = outbox._execute

        async def interrupt_marker(sql, params=()):
            if "INSERT INTO telemetry_contract_recoveries" in sql:
                raise asyncio.CancelledError
            return await execute(sql, params)

        monkeypatch.setattr(outbox, "_execute", interrupt_marker)
        with pytest.raises(asyncio.CancelledError):
            await outbox.recover_contract_rejection_once(event)
        assert (await outbox.stats()).pending_events == 0
        monkeypatch.setattr(outbox, "_execute", execute)
        assert await outbox.recover_contract_rejection_once(event)
    finally:
        await outbox.close()


async def test_concurrent_processes_recover_one_event_only_once(tmp_path):
    event = _device_activity()
    clock = FakeClock(int(event.occurred_at_utc.timestamp() * 1000) + 1000)
    outbox = await _open(tmp_path, TelemetryScope.GROWTH, clock=clock)
    await _reject_activity(outbox, event)
    await outbox.close()
    script = """
import asyncio, sys
from pathlib import Path
from opensquilla.telemetry.contracts import TELEMETRY_EVENT_ADAPTER
from opensquilla.telemetry import outbox as outbox_module
from opensquilla.telemetry.outbox import TelemetryOutbox
assert Path(outbox_module.__file__).resolve().is_relative_to(Path(sys.argv[4]).resolve())
async def run():
    event = TELEMETRY_EVENT_ADAPTER.validate_json(sys.argv[2], strict=True)
    outbox = await TelemetryOutbox.open(sys.argv[1], 'growth', clock=lambda: int(sys.argv[3]))
    async with outbox:
        print(int(await outbox.recover_contract_rejection_once(event)))
asyncio.run(run())
"""
    processes = await asyncio.gather(*(
        asyncio.create_subprocess_exec(
            sys.executable, "-c", script, str(tmp_path), event.model_dump_json(), str(clock.now_ms),
            str(Path(__file__).resolve().parents[2] / "src"),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        ) for _ in range(4)
    ))
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(process.communicate() for process in processes)), timeout=30,
        )
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
        await asyncio.gather(*(process.wait() for process in processes))
    for process, (_, error) in zip(processes, results, strict=True):
        assert process.returncode == 0, error.decode()
    assert sorted(output.strip() for output, _ in results) == [b"0", b"0", b"0", b"1"]
    outbox = await _open(tmp_path, TelemetryScope.GROWTH, clock=clock)
    try:
        assert (await outbox.stats()).pending_events == 1
    finally:
        await outbox.close()


async def test_scope_clear_does_not_touch_other_physical_database(tmp_path: Path) -> None:
    reliability = await _open(tmp_path, TelemetryScope.RELIABILITY)
    growth = await _open(tmp_path, TelemetryScope.GROWTH)
    try:
        await reliability.enqueue(_turn_event())
        await growth.enqueue(_growth_event())

        assert await reliability.clear_scope() == 1
        assert (await reliability.stats()).pending_events == 0
        assert (await growth.stats()).pending_events == 1
    finally:
        await reliability.close()
        await growth.close()


async def test_claim_obeys_scope_batch_count_and_exact_body_limit(tmp_path: Path) -> None:
    limits = OutboxLimits(batch_max_events=2, batch_max_bytes=1_500)
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY, limits=limits)
    try:
        for number in range(1, 4):
            await outbox.enqueue(_turn_event(number))

        batch = await outbox.claim_batch()
        assert batch is not None
        assert len(batch.events) == 2
        assert len(batch.body) <= 1_500
        assert json.loads(batch.body)["batch_version"] == 1
        assert (await outbox.stats()).pending_events == 3
    finally:
        await outbox.close()


@pytest.mark.parametrize(
    ("column", "tampered_value"),
    [
        ("payload_sha256", b"\x00" * 32),
        ("payload_bytes", 0),
        ("event_id", _uuid(999)),
        ("event_name", "tool_call_result"),
        ("event_version", 2),
    ],
)
async def test_claim_deletes_digest_or_metadata_mismatch_and_keeps_valid_peer(
    tmp_path: Path,
    column: str,
    tampered_value: object,
) -> None:
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY)
    try:
        await outbox.enqueue(_turn_event(1))
        await outbox.enqueue(_turn_event(2))
        update_statements = {
            "payload_sha256": (
                "UPDATE telemetry_outbox SET payload_sha256 = ? WHERE event_id = ?"
            ),
            "payload_bytes": (
                "UPDATE telemetry_outbox SET payload_bytes = ? WHERE event_id = ?"
            ),
            "event_id": "UPDATE telemetry_outbox SET event_id = ? WHERE event_id = ?",
            "event_name": "UPDATE telemetry_outbox SET event_name = ? WHERE event_id = ?",
            "event_version": (
                "UPDATE telemetry_outbox SET event_version = ? WHERE event_id = ?"
            ),
        }
        with sqlite3.connect(outbox.database_path) as connection:
            connection.execute(
                update_statements[column],
                (tampered_value, _uuid(1)),
            )

        batch = await outbox.claim_batch()
        assert batch is not None
        assert [event.event_id for event in batch.events] == [_uuid(2)]
        assert (await outbox.stats()).pending_events == 1
    finally:
        await outbox.close()


async def test_claim_deletes_corrupt_payload_even_with_matching_digest_and_size(
    tmp_path: Path,
) -> None:
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY)
    corrupt_payload = b'{"event_name":"turn_result"}'
    try:
        await outbox.enqueue(_turn_event())
        with sqlite3.connect(outbox.database_path) as connection:
            connection.execute(
                """
                UPDATE telemetry_outbox
                SET payload = ?, payload_sha256 = ?, payload_bytes = ?
                WHERE event_id = ?
                """,
                (
                    corrupt_payload,
                    hashlib.sha256(corrupt_payload).digest(),
                    len(corrupt_payload),
                    _uuid(1),
                ),
            )

        assert await outbox.claim_batch() is None
        assert (await outbox.stats()).pending_events == 0
    finally:
        await outbox.close()


async def test_claim_rejects_opposite_scope_payload_with_matching_row_metadata(
    tmp_path: Path,
) -> None:
    outbox = await _open(tmp_path, TelemetryScope.RELIABILITY)
    growth_payload = canonical_json_bytes(_growth_event())
    try:
        await outbox.enqueue(_turn_event())
        with sqlite3.connect(outbox.database_path) as connection:
            connection.execute(
                """
                UPDATE telemetry_outbox
                SET event_name = ?, payload = ?, payload_sha256 = ?, payload_bytes = ?
                WHERE event_id = ?
                """,
                (
                    "first_app_ready",
                    growth_payload,
                    hashlib.sha256(growth_payload).digest(),
                    len(growth_payload),
                    _uuid(1),
                ),
            )

        assert await outbox.claim_batch() is None
        assert (await outbox.stats()).pending_events == 0
    finally:
        await outbox.close()


@pytest.mark.parametrize(
    ("scope", "event_factory", "target"),
    [
        (
            TelemetryScope.RELIABILITY,
            _turn_event,
            TelemetryWireTarget.RELIABILITY_BATCH,
        ),
        (TelemetryScope.GROWTH, _growth_event, TelemetryWireTarget.GROWTH_BATCH),
    ],
)
async def test_claimed_body_is_strictly_valid_and_limits_come_from_manifest(
    tmp_path: Path,
    scope: TelemetryScope,
    event_factory: Callable[[], StrictTelemetryModel],
    target: TelemetryWireTarget,
) -> None:
    outbox = await _open(tmp_path, scope)
    try:
        await outbox.enqueue(event_factory())
        batch = await outbox.claim_batch()
        assert batch is not None
        parsed = parse_telemetry_wire(batch.body, target=target)
        assert str(parsed.events[0].event_id) == batch.events[0].event_id
        assert batch.events[0].event_version == 1

        scope_limits = telemetry_protocol_manifest()["batch_limits"][scope.value]
        assert outbox.limits.batch_max_events == scope_limits["max_events"]
        assert outbox.limits.batch_max_bytes == scope_limits["max_bytes"]
    finally:
        await outbox.close()
