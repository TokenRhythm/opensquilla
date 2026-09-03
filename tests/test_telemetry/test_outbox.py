from __future__ import annotations

import hashlib
import json
import os
import sqlite3
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
        "notice_version": "growth-v1",
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
