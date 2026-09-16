"""Durable, scope-isolated telemetry upload queues.

Only validated telemetry model instances cross this boundary.  Each scope owns
its own SQLite database so consent withdrawal, retention, and operational
failures cannot couple reliability diagnostics to growth analytics.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

from opensquilla.compat import aiosqlite
from opensquilla.telemetry.consent import TelemetryScope
from opensquilla.telemetry.contracts import (
    EVENT_MODELS,
    MAX_TELEMETRY_EVENT_BYTES,
    TelemetryWireError,
    TelemetryWireTarget,
    canonical_json_bytes,
    parse_telemetry_wire,
    telemetry_protocol_manifest,
)
from opensquilla.telemetry.contracts.common import (
    BATCH_VERSION,
    EventBase,
    StrictTelemetryModel,
)
from opensquilla.telemetry.ids import new_batch_id

_DEFAULT_MAX_EVENTS = 10_000
_DEFAULT_MAX_PAYLOAD_BYTES = 10 * 1024 * 1024
_DEFAULT_TTL_MS = 30 * 24 * 60 * 60 * 1_000
_DEFAULT_LEASE_MS = 60_000
_DEFAULT_BUSY_TIMEOUT_MS = 5_000
_DEFAULT_REJECTION_MAX_EVENTS = 1_000
_DEFAULT_REJECTION_TTL_MS = 30 * 24 * 60 * 60 * 1_000
_BATCH_ENVELOPE_RESERVE_BYTES = 256

_SCOPE_DATABASE_NAMES = {
    TelemetryScope.RELIABILITY: "reliability-outbox.sqlite3",
    TelemetryScope.GROWTH: "growth-outbox.sqlite3",
}


def _load_scope_batch_limits() -> dict[TelemetryScope, tuple[int, int]]:
    manifest = telemetry_protocol_manifest()
    raw_limits = manifest.get("batch_limits")
    if not isinstance(raw_limits, dict):  # pragma: no cover - constant invariant
        raise RuntimeError("telemetry protocol manifest has invalid batch limits")

    resolved: dict[TelemetryScope, tuple[int, int]] = {}
    for scope in TelemetryScope:
        raw_scope = raw_limits.get(scope.value)
        if not isinstance(raw_scope, dict):  # pragma: no cover - constant invariant
            raise RuntimeError("telemetry protocol manifest has invalid scope limits")
        max_events = raw_scope.get("max_events")
        max_bytes = raw_scope.get("max_bytes")
        if (
            type(max_events) is not int
            or max_events <= 0
            or type(max_bytes) is not int
            or max_bytes <= 0
        ):  # pragma: no cover - constant invariant
            raise RuntimeError("telemetry protocol manifest has invalid scope limits")
        resolved[scope] = (max_events, max_bytes)
    return resolved


_SCOPE_BATCH_LIMITS = _load_scope_batch_limits()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry_outbox (
    event_id TEXT PRIMARY KEY,
    event_name TEXT NOT NULL,
    event_version INTEGER NOT NULL,
    payload BLOB NOT NULL,
    payload_sha256 BLOB NOT NULL,
    payload_bytes INTEGER NOT NULL CHECK(payload_bytes >= 0),
    priority INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    next_attempt_at_ms INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    lease_id TEXT,
    lease_until_ms INTEGER
);

CREATE INDEX IF NOT EXISTS telemetry_outbox_claim_idx
ON telemetry_outbox(next_attempt_at_ms, lease_until_ms, priority, created_at_ms);

CREATE INDEX IF NOT EXISTS telemetry_outbox_expiry_idx
ON telemetry_outbox(expires_at_ms);

CREATE TABLE IF NOT EXISTS telemetry_rejections (
    event_id TEXT PRIMARY KEY,
    event_name TEXT NOT NULL,
    rejected_at_ms INTEGER NOT NULL,
    status_code INTEGER NOT NULL,
    reason TEXT NOT NULL
);
"""


class OutboxPriority(IntEnum):
    LOW = 10
    NORMAL = 50
    HIGH = 80
    CRITICAL = 100


class EnqueueResult(StrEnum):
    ENQUEUED = "enqueued"
    DUPLICATE = "duplicate"
    EVICTED = "evicted"


class QuarantineReason(StrEnum):
    CONFLICT = "conflict"
    CONTRACT = "contract"


class OutboxEventConflictError(ValueError):
    """An event ID already exists with different validated content."""


class OutboxPayloadTooLargeError(ValueError):
    """One event cannot fit within its scope's upload body limit."""


@dataclass(frozen=True)
class OutboxLimits:
    max_events: int = _DEFAULT_MAX_EVENTS
    max_payload_bytes: int = _DEFAULT_MAX_PAYLOAD_BYTES
    ttl_ms: int = _DEFAULT_TTL_MS
    lease_ms: int = _DEFAULT_LEASE_MS
    busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS
    rejection_max_events: int = _DEFAULT_REJECTION_MAX_EVENTS
    rejection_ttl_ms: int = _DEFAULT_REJECTION_TTL_MS
    batch_max_events: int | None = None
    batch_max_bytes: int | None = None

    def __post_init__(self) -> None:
        values = {
            "max_events": self.max_events,
            "max_payload_bytes": self.max_payload_bytes,
            "ttl_ms": self.ttl_ms,
            "lease_ms": self.lease_ms,
            "busy_timeout_ms": self.busy_timeout_ms,
            "rejection_max_events": self.rejection_max_events,
            "rejection_ttl_ms": self.rejection_ttl_ms,
        }
        if self.batch_max_events is not None:
            values["batch_max_events"] = self.batch_max_events
        if self.batch_max_bytes is not None:
            values["batch_max_bytes"] = self.batch_max_bytes
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class ClaimedEvent:
    event_id: str
    event_name: str
    event_version: int
    notice_version: str
    payload: bytes
    attempt_count: int
    priority: OutboxPriority


@dataclass(frozen=True)
class ClaimedBatch:
    scope: TelemetryScope
    lease_id: str
    batch_id: str
    sent_at_utc: str
    events: tuple[ClaimedEvent, ...]
    body: bytes


@dataclass(frozen=True)
class OutboxStats:
    pending_events: int
    leased_events: int
    payload_bytes: int
    rejected_events: int


@dataclass(frozen=True)
class RejectedEvent:
    event_id: str
    event_name: str
    rejected_at_ms: int
    status_code: int
    reason: str


class TelemetryOutbox:
    """One asynchronous SQLite queue bound to exactly one telemetry scope."""

    def __init__(
        self,
        *,
        scope: TelemetryScope,
        database_path: Path,
        connection: aiosqlite.Connection,
        limits: OutboxLimits,
        clock: Callable[[], int],
    ) -> None:
        self.scope = scope
        self.database_path = database_path
        self.limits = limits
        self._connection = connection
        self._clock = clock
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(
        cls,
        state_dir: str | Path,
        scope: TelemetryScope | str,
        *,
        limits: OutboxLimits | None = None,
        clock: Callable[[], int] | None = None,
    ) -> Self:
        normalized_scope = TelemetryScope(scope)
        resolved_limits = _resolve_limits(normalized_scope, limits or OutboxLimits())
        directory = Path(state_dir) / "telemetry"
        if directory.is_symlink():
            raise ValueError("telemetry state directory cannot be a symlink")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _restrict_permissions(directory, 0o700)
        database_path = directory / _SCOPE_DATABASE_NAMES[normalized_scope]
        if database_path.is_symlink():
            raise ValueError("telemetry outbox database cannot be a symlink")

        connection = await aiosqlite.connect(
            str(database_path),
            isolation_level=None,
            timeout=resolved_limits.busy_timeout_ms / 1_000,
        )
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute("PRAGMA foreign_keys=ON")
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute("PRAGMA synchronous=FULL")
            await connection.execute(f"PRAGMA busy_timeout={resolved_limits.busy_timeout_ms}")
            await connection.executescript(_SCHEMA)
            await connection.execute("PRAGMA user_version=1")
            _restrict_permissions(database_path, 0o600)
        except BaseException:
            await connection.close()
            raise

        return cls(
            scope=normalized_scope,
            database_path=database_path,
            connection=connection,
            limits=resolved_limits,
            clock=clock or _now_ms,
        )

    async def __aenter__(self) -> Self:
        self._ensure_open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._connection.close()

    async def enqueue(
        self,
        event: StrictTelemetryModel,
        *,
        priority: OutboxPriority | int | None = None,
    ) -> EnqueueResult:
        """Revalidate and durably enqueue one typed event."""

        self._ensure_open()
        if not isinstance(event, StrictTelemetryModel):
            raise TypeError("outbox accepts only validated telemetry models")
        event_name = getattr(event, "event_name", None)
        event_version = getattr(event, "event_version", None)
        if (
            not isinstance(event_name, str)
            or isinstance(event_version, bool)
            or not isinstance(event_version, int)
        ):
            raise TypeError("outbox accepts only registered telemetry event models")
        expected_model = EVENT_MODELS.get((event_name, event_version))
        if expected_model is None or not isinstance(event, expected_model):
            raise TypeError("outbox accepts only registered telemetry event models")
        validated = expected_model.model_validate(event, strict=True)
        consent_scope = str(getattr(validated, "consent_scope", ""))
        if consent_scope != self.scope.value:
            raise ValueError("event consent scope does not match outbox scope")

        payload = canonical_json_bytes(validated)
        batch_max_bytes = self.limits.batch_max_bytes
        assert batch_max_bytes is not None
        if (
            len(payload) > MAX_TELEMETRY_EVENT_BYTES
            or len(payload) + _BATCH_ENVELOPE_RESERVE_BYTES > batch_max_bytes
        ):
            raise OutboxPayloadTooLargeError("event exceeds scope batch body limit")
        payload_digest = hashlib.sha256(payload).digest()
        event_id = str(getattr(validated, "event_id"))
        normalized_priority = (
            OutboxPriority(priority) if priority is not None else _priority_for(validated)
        )
        now_ms = self._clock()

        async with self._transaction():
            await self._purge_expired_locked(now_ms)
            existing = await self._fetchone(
                "SELECT payload_sha256 FROM telemetry_outbox WHERE event_id = ?",
                (event_id,),
            )
            if existing is not None:
                if bytes(existing["payload_sha256"]) == payload_digest:
                    return EnqueueResult.DUPLICATE
                raise OutboxEventConflictError("event_id already has different content")

            await self._execute(
                """
                INSERT INTO telemetry_outbox (
                    event_id, event_name, event_version, payload, payload_sha256,
                    payload_bytes, priority, created_at_ms, expires_at_ms,
                    next_attempt_at_ms, attempt_count, lease_id, lease_until_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
                """,
                (
                    event_id,
                    str(event_name),
                    int(event_version),
                    payload,
                    payload_digest,
                    len(payload),
                    int(normalized_priority),
                    now_ms,
                    now_ms + self.limits.ttl_ms,
                    now_ms,
                ),
            )
            await self._evict_over_capacity_locked(now_ms)
            retained = await self._fetchone(
                "SELECT 1 FROM telemetry_outbox WHERE event_id = ?",
                (event_id,),
            )
            return EnqueueResult.ENQUEUED if retained is not None else EnqueueResult.EVICTED

    async def claim_batch(self) -> ClaimedBatch | None:
        """Atomically lease the next bounded upload batch."""

        self._ensure_open()
        now_ms = self._clock()
        lease_id = uuid4().hex
        batch_id = str(new_batch_id())
        sent_at_utc = _utc_ms(now_ms)
        batch_max_events = self.limits.batch_max_events
        batch_max_bytes = self.limits.batch_max_bytes
        assert batch_max_events is not None and batch_max_bytes is not None

        async with self._transaction():
            await self._purge_expired_locked(now_ms)
            rows = await self._fetchall(
                """
                SELECT rowid AS queue_rowid,
                       event_id,
                       event_name,
                       event_version,
                       payload_bytes,
                       length(payload) AS actual_payload_bytes,
                       length(payload_sha256) AS actual_digest_bytes,
                       attempt_count,
                       priority
                FROM telemetry_outbox
                WHERE next_attempt_at_ms <= ?
                  AND expires_at_ms > ?
                  AND (lease_id IS NULL OR lease_until_ms <= ?)
                ORDER BY priority DESC, created_at_ms ASC, event_id ASC
                LIMIT ?
                """,
                (now_ms, now_ms, now_ms, batch_max_events),
            )
            selected: list[ClaimedEvent] = []
            for row in rows:
                candidate: ClaimedEvent | None = None
                if _claim_row_metadata_is_valid(row):
                    stored = await self._fetchone(
                        """
                        SELECT payload, payload_sha256
                        FROM telemetry_outbox
                        WHERE rowid = ?
                        """,
                        (row["queue_rowid"],),
                    )
                    if stored is not None:
                        candidate = _validated_claimed_event(
                            row,
                            raw_payload=stored["payload"],
                            raw_digest=stored["payload_sha256"],
                            scope=self.scope,
                        )
                if candidate is None:
                    await self._execute(
                        "DELETE FROM telemetry_outbox WHERE rowid = ?",
                        (row["queue_rowid"],),
                    )
                    continue
                candidate_body = _batch_body(
                    batch_id=batch_id,
                    sent_at_utc=sent_at_utc,
                    payloads=tuple(event.payload for event in (*selected, candidate)),
                )
                if len(candidate_body) > batch_max_bytes:
                    break
                selected.append(candidate)

            if not selected:
                return None

            body = _batch_body(
                batch_id=batch_id,
                sent_at_utc=sent_at_utc,
                payloads=tuple(event.payload for event in selected),
            )
            _validate_constructed_batch(body, scope=self.scope)

            event_ids = tuple(event.event_id for event in selected)
            placeholders = ",".join("?" for _ in event_ids)
            await self._execute(
                f"""
                UPDATE telemetry_outbox
                SET lease_id = ?, lease_until_ms = ?, attempt_count = attempt_count + 1
                WHERE event_id IN ({placeholders})
                """,
                (lease_id, now_ms + self.limits.lease_ms, *event_ids),
            )
            return ClaimedBatch(
                scope=self.scope,
                lease_id=lease_id,
                batch_id=batch_id,
                sent_at_utc=sent_at_utc,
                events=tuple(selected),
                body=body,
            )

    async def acknowledge(self, lease_id: str) -> int:
        """Delete every event still owned by *lease_id*."""

        self._ensure_open()
        async with self._transaction():
            return await self._execute(
                "DELETE FROM telemetry_outbox WHERE lease_id = ?",
                (lease_id,),
            )

    async def release_for_retry(self, lease_id: str, *, next_attempt_at_ms: int) -> int:
        """Release one failed batch and preserve its incremented attempt count."""

        self._ensure_open()
        if isinstance(next_attempt_at_ms, bool) or next_attempt_at_ms < 0:
            raise ValueError("next_attempt_at_ms must be non-negative")
        async with self._transaction():
            return await self._execute(
                """
                UPDATE telemetry_outbox
                SET lease_id = NULL, lease_until_ms = NULL, next_attempt_at_ms = ?
                WHERE lease_id = ?
                """,
                (next_attempt_at_ms, lease_id),
            )

    async def release_unattempted(self, lease_id: str) -> int:
        """Undo a claim when consent closes before the HTTP request starts."""

        self._ensure_open()
        async with self._transaction():
            return await self._execute(
                """
                UPDATE telemetry_outbox
                SET lease_id = NULL,
                    lease_until_ms = NULL,
                    next_attempt_at_ms = ?,
                    attempt_count = MAX(attempt_count - 1, 0)
                WHERE lease_id = ?
                """,
                (self._clock(), lease_id),
            )

    async def discard_claimed_events(
        self,
        lease_id: str,
        event_ids: tuple[str, ...],
        *,
        release_remaining: bool = True,
    ) -> int:
        """Delete selected leased events and optionally release their peers.

        This is used when a claimed batch contains events authorized under an
        obsolete notice.  Deletion and release share one transaction so a
        current-notice event is never stranded behind the discarded lease.
        No rejection metadata is retained because consent/notice expiry is a
        local privacy decision, not a server-side contract failure.
        """

        self._ensure_open()
        normalized_ids = tuple(dict.fromkeys(event_ids))
        if not normalized_ids or any(
            not isinstance(value, str) or not value for value in normalized_ids
        ):
            raise ValueError("event_ids must contain at least one non-empty string")

        placeholders = ",".join("?" for _ in normalized_ids)
        async with self._transaction():
            removed = await self._execute(
                f"""
                DELETE FROM telemetry_outbox
                WHERE lease_id = ? AND event_id IN ({placeholders})
                """,
                (lease_id, *normalized_ids),
            )
            if release_remaining:
                await self._execute(
                    """
                    UPDATE telemetry_outbox
                    SET lease_id = NULL,
                        lease_until_ms = NULL,
                        next_attempt_at_ms = ?,
                        attempt_count = MAX(attempt_count - 1, 0)
                    WHERE lease_id = ?
                    """,
                    (self._clock(), lease_id),
                )
            return removed

    async def quarantine(
        self,
        lease_id: str,
        *,
        status_code: int,
        reason: QuarantineReason | str,
    ) -> int:
        """Replace rejected payloads with bounded, non-payload metadata."""

        self._ensure_open()
        normalized_reason = QuarantineReason(reason)
        if isinstance(status_code, bool) or status_code not in {409, 422}:
            raise ValueError("only permanent telemetry rejection statuses can be quarantined")
        now_ms = self._clock()
        async with self._transaction():
            rows = await self._fetchall(
                "SELECT event_id, event_name FROM telemetry_outbox WHERE lease_id = ?",
                (lease_id,),
            )
            for row in rows:
                await self._execute(
                    """
                    INSERT INTO telemetry_rejections (
                        event_id, event_name, rejected_at_ms, status_code, reason
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        event_name = excluded.event_name,
                        rejected_at_ms = excluded.rejected_at_ms,
                        status_code = excluded.status_code,
                        reason = excluded.reason
                    """,
                    (
                        str(row["event_id"]),
                        str(row["event_name"]),
                        now_ms,
                        status_code,
                        normalized_reason.value,
                    ),
                )
            await self._execute(
                "DELETE FROM telemetry_outbox WHERE lease_id = ?",
                (lease_id,),
            )
            await self._purge_rejections_locked(now_ms)
            await self._trim_rejections_locked()
            return len(rows)

    async def clear_scope(self) -> int:
        """Erase this scope's pending payloads and rejection metadata."""

        self._ensure_open()
        async with self._transaction():
            removed = await self._execute("DELETE FROM telemetry_outbox")
            await self._execute("DELETE FROM telemetry_rejections")
            return removed

    async def stats(self) -> OutboxStats:
        self._ensure_open()
        now_ms = self._clock()
        async with self._transaction():
            await self._purge_expired_locked(now_ms)
            row = await self._fetchone(
                """
                SELECT
                    COUNT(*) AS pending_events,
                    COALESCE(SUM(CASE WHEN lease_id IS NOT NULL THEN 1 ELSE 0 END), 0)
                        AS leased_events,
                    COALESCE(SUM(payload_bytes), 0) AS payload_bytes
                FROM telemetry_outbox
                """
            )
            rejected = await self._fetchone(
                "SELECT COUNT(*) AS rejected_events FROM telemetry_rejections"
            )
            assert row is not None and rejected is not None
            return OutboxStats(
                pending_events=int(row["pending_events"]),
                leased_events=int(row["leased_events"]),
                payload_bytes=int(row["payload_bytes"]),
                rejected_events=int(rejected["rejected_events"]),
            )

    async def list_rejections(self) -> tuple[RejectedEvent, ...]:
        """Return payload-free rejection metadata for diagnostics/tests."""

        self._ensure_open()
        async with self._transaction():
            await self._purge_rejections_locked(self._clock())
            rows = await self._fetchall(
                """
                SELECT event_id, event_name, rejected_at_ms, status_code, reason
                FROM telemetry_rejections
                ORDER BY rejected_at_ms ASC, event_id ASC
                """
            )
        return tuple(
            RejectedEvent(
                event_id=str(row["event_id"]),
                event_name=str(row["event_name"]),
                rejected_at_ms=int(row["rejected_at_ms"]),
                status_code=int(row["status_code"]),
                reason=str(row["reason"]),
            )
            for row in rows
        )

    @asynccontextmanager
    async def _transaction(self):
        async with self._lock:
            await self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                await self._connection.commit()
            except BaseException:
                await self._connection.rollback()
                raise

    async def _purge_expired_locked(self, now_ms: int) -> int:
        removed = await self._execute(
            "DELETE FROM telemetry_outbox WHERE expires_at_ms <= ?",
            (now_ms,),
        )
        await self._purge_rejections_locked(now_ms)
        return removed

    async def _purge_rejections_locked(self, now_ms: int) -> int:
        return await self._execute(
            "DELETE FROM telemetry_rejections WHERE rejected_at_ms <= ?",
            (now_ms - self.limits.rejection_ttl_ms,),
        )

    async def _trim_rejections_locked(self) -> int:
        return await self._execute(
            """
            DELETE FROM telemetry_rejections
            WHERE event_id IN (
                SELECT event_id
                FROM telemetry_rejections
                ORDER BY rejected_at_ms DESC, event_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self.limits.rejection_max_events,),
        )

    async def _evict_over_capacity_locked(self, now_ms: int) -> None:
        totals = await self._fetchone(
            "SELECT COUNT(*) AS count, COALESCE(SUM(payload_bytes), 0) AS bytes "
            "FROM telemetry_outbox"
        )
        assert totals is not None
        count = int(totals["count"])
        payload_bytes = int(totals["bytes"])
        if count <= self.limits.max_events and payload_bytes <= self.limits.max_payload_bytes:
            return

        candidates = await self._fetchall(
            """
            SELECT event_id, payload_bytes
            FROM telemetry_outbox
            WHERE lease_id IS NULL OR lease_until_ms <= ?
            ORDER BY priority ASC, created_at_ms ASC, event_id ASC
            """,
            (now_ms,),
        )
        for row in candidates:
            if count <= self.limits.max_events and payload_bytes <= self.limits.max_payload_bytes:
                break
            removed = await self._execute(
                "DELETE FROM telemetry_outbox WHERE event_id = ?",
                (str(row["event_id"]),),
            )
            if removed:
                count -= 1
                payload_bytes -= int(row["payload_bytes"])

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        cursor = await self._connection.execute(sql, params)
        try:
            return max(int(cursor.rowcount), 0)
        finally:
            await cursor.close()

    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        cursor = await self._connection.execute(sql, params)
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        cursor = await self._connection.execute(sql, params)
        try:
            return await cursor.fetchall()
        finally:
            await cursor.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("telemetry outbox is closed")


def _resolve_limits(scope: TelemetryScope, limits: OutboxLimits) -> OutboxLimits:
    default_events, default_bytes = _SCOPE_BATCH_LIMITS[scope]
    return replace(
        limits,
        batch_max_events=min(limits.batch_max_events or default_events, default_events),
        batch_max_bytes=min(limits.batch_max_bytes or default_bytes, default_bytes),
    )


def _claim_row_metadata_is_valid(row: Any) -> bool:
    row_event_id = row["event_id"]
    row_event_name = row["event_name"]
    row_event_version = row["event_version"]
    row_payload_bytes = row["payload_bytes"]
    actual_payload_bytes = row["actual_payload_bytes"]
    actual_digest_bytes = row["actual_digest_bytes"]
    row_attempt_count = row["attempt_count"]
    row_priority = row["priority"]
    return not (
        not isinstance(row_event_id, str)
        or not row_event_id
        or not isinstance(row_event_name, str)
        or not row_event_name
        or type(row_event_version) is not int
        or type(row_payload_bytes) is not int
        or row_payload_bytes < 0
        or row_payload_bytes > MAX_TELEMETRY_EVENT_BYTES
        or type(actual_payload_bytes) is not int
        or actual_payload_bytes != row_payload_bytes
        or actual_digest_bytes != hashlib.sha256().digest_size
        or type(row_attempt_count) is not int
        or row_attempt_count < 0
        or type(row_priority) is not int
    )


def _validated_claimed_event(
    row: Any,
    *,
    raw_payload: Any,
    raw_digest: Any,
    scope: TelemetryScope,
) -> ClaimedEvent | None:
    """Fail closed when persisted queue bytes or their metadata were altered."""

    row_event_id = row["event_id"]
    row_event_name = row["event_name"]
    row_event_version = row["event_version"]
    row_attempt_count = row["attempt_count"]
    row_priority = row["priority"]
    if not isinstance(raw_payload, (bytes, bytearray, memoryview)) or not isinstance(
        raw_digest,
        (bytes, bytearray, memoryview),
    ):
        return None
    try:
        payload = bytes(raw_payload)
        stored_digest = bytes(raw_digest)
        priority = OutboxPriority(row_priority)
    except (TypeError, ValueError):
        return None
    if row["payload_bytes"] != len(payload):
        return None
    if not hmac.compare_digest(stored_digest, hashlib.sha256(payload).digest()):
        return None

    try:
        parsed: EventBase
        if scope is TelemetryScope.RELIABILITY:
            parsed = parse_telemetry_wire(
                payload,
                target=TelemetryWireTarget.RELIABILITY_EVENT,
            )
        else:
            parsed = parse_telemetry_wire(
                payload,
                target=TelemetryWireTarget.GROWTH_EVENT,
            )
    except TelemetryWireError:
        return None

    if (
        canonical_json_bytes(parsed) != payload
        or str(parsed.event_id) != row_event_id
        or parsed.event_name != row_event_name
        or parsed.event_version != row_event_version
        or str(parsed.consent_scope) != scope.value
    ):
        return None

    return ClaimedEvent(
        event_id=row_event_id,
        event_name=row_event_name,
        event_version=row_event_version,
        notice_version=str(parsed.notice_version),
        payload=payload,
        attempt_count=row_attempt_count + 1,
        priority=priority,
    )


def _validate_constructed_batch(body: bytes, *, scope: TelemetryScope) -> None:
    """Apply the public raw-wire parser at the final pre-lease boundary."""

    try:
        if scope is TelemetryScope.RELIABILITY:
            parse_telemetry_wire(
                body,
                target=TelemetryWireTarget.RELIABILITY_BATCH,
            )
        else:
            parse_telemetry_wire(
                body,
                target=TelemetryWireTarget.GROWTH_BATCH,
            )
    except TelemetryWireError as exc:  # pragma: no cover - construction invariant
        raise RuntimeError("constructed telemetry batch failed validation") from exc


def _priority_for(event: StrictTelemetryModel) -> OutboxPriority:
    event_name = str(getattr(event, "event_name", ""))
    outcome = str(getattr(event, "outcome", ""))
    if event_name == "app_crash_detected":
        return OutboxPriority.CRITICAL
    if outcome in {"fail", "timeout"}:
        return OutboxPriority.HIGH
    if event_name == "performance_summary":
        return OutboxPriority.LOW
    return OutboxPriority.NORMAL


def _batch_body(*, batch_id: str, sent_at_utc: str, payloads: tuple[bytes, ...]) -> bytes:
    batch_id_json = json.dumps(batch_id, ensure_ascii=True, separators=(",", ":"))
    sent_at_json = json.dumps(sent_at_utc, ensure_ascii=True, separators=(",", ":"))
    batch_version_json = str(BATCH_VERSION).encode("ascii")
    return (
        b'{"batch_id":'
        + batch_id_json.encode("ascii")
        + b',"batch_version":'
        + batch_version_json
        + b',"events":['
        + b",".join(payloads)
        + b'],"sent_at_utc":'
        + sent_at_json.encode("ascii")
        + b"}"
    )


def _utc_ms(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1_000, tz=UTC)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1_000)


def _restrict_permissions(path: Path, mode: int) -> None:
    if os.name == "nt":
        return
    path.chmod(mode)


__all__ = [
    "ClaimedBatch",
    "ClaimedEvent",
    "EnqueueResult",
    "OutboxEventConflictError",
    "OutboxLimits",
    "OutboxPayloadTooLargeError",
    "OutboxPriority",
    "OutboxStats",
    "QuarantineReason",
    "RejectedEvent",
    "TelemetryOutbox",
]
