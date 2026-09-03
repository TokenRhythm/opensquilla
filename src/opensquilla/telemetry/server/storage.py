"""Transactional, scope-isolated SQLite storage for accepted telemetry batches."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

import aiosqlite

from opensquilla.telemetry.contracts.batch import (
    GrowthEventBatch,
    ReliabilityEventBatch,
    TelemetryBatch,
)
from opensquilla.telemetry.contracts.canonical import canonical_json_bytes
from opensquilla.telemetry.contracts.common import ConsentScope, StrictTelemetryModel
from opensquilla.telemetry.contracts.manifest import (
    TELEMETRY_PROTOCOL_FINGERPRINT_SHA256,
)

SCHEMA_VERSION = 1
_LEGACY_PROTOCOL_FINGERPRINT_SHA256 = (
    "1980ad6ba1a5db2f4b620e019e32b45e398a90699b708364f4713e411324f099"
)
_PREVIOUS_PROTOCOL_FINGERPRINT_SHA256 = (
    "0e7769f58ef3d9824c30fcc3a7dd0681afa3553fa8dac1eba453172e5dbe3cb5"
)

_EXPECTED_TABLES = frozenset({"events", "ingest_batches", "meta"})
_EXPECTED_COLUMNS = {
    "meta": (
        "singleton",
        "schema_version",
        "scope",
        "protocol_fingerprint",
        "created_at_utc",
    ),
    "ingest_batches": (
        "batch_id",
        "body_sha256",
        "sent_at_utc",
        "received_at_utc",
        "accepted_count",
        "duplicate_count",
    ),
    "events": (
        "event_id",
        "payload_sha256",
        "event_name",
        "event_version",
        "occurred_at_utc",
        "source",
        "app_version",
        "platform",
        "outcome",
        "error_code",
        "duration_ms",
        "sample_rate",
        "notice_version",
        "app_session_id",
        "acquisition_id",
        "analytics_user_id",
        "payload_json",
        "first_batch_id",
        "received_at_utc",
    ),
}
_EXPECTED_INDEXES = frozenset(
    {
        "idx_events_acquisition_name_occurred",
        "idx_events_analytics_user_name_occurred",
        "idx_events_app_session_name",
        "idx_events_app_version_occurred",
        "idx_events_name_occurred",
        "idx_events_occurred",
        "idx_events_outcome_error_occurred",
        "idx_events_received",
        "idx_events_client_launch_user_surface_day",
    }
)
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE meta (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        scope TEXT NOT NULL CHECK (scope IN ('reliability', 'growth')),
        protocol_fingerprint TEXT NOT NULL CHECK (
            length(protocol_fingerprint) = 64
            AND protocol_fingerprint NOT GLOB '*[^a-f0-9]*'
        ),
        created_at_utc TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE ingest_batches (
        batch_id TEXT PRIMARY KEY NOT NULL CHECK (length(batch_id) = 36),
        body_sha256 TEXT NOT NULL CHECK (
            length(body_sha256) = 64 AND body_sha256 NOT GLOB '*[^a-f0-9]*'
        ),
        sent_at_utc TEXT NOT NULL,
        received_at_utc TEXT NOT NULL,
        accepted_count INTEGER NOT NULL CHECK (accepted_count >= 0),
        duplicate_count INTEGER NOT NULL CHECK (duplicate_count >= 0)
    ) STRICT
    """,
    """
    CREATE TABLE events (
        event_id TEXT PRIMARY KEY NOT NULL CHECK (length(event_id) = 36),
        payload_sha256 TEXT NOT NULL CHECK (
            length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^a-f0-9]*'
        ),
        event_name TEXT NOT NULL,
        event_version INTEGER NOT NULL CHECK (event_version >= 1),
        occurred_at_utc TEXT NOT NULL,
        source TEXT NOT NULL,
        app_version TEXT,
        platform TEXT NOT NULL,
        outcome TEXT,
        error_code TEXT,
        duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
        sample_rate REAL NOT NULL CHECK (sample_rate > 0.0 AND sample_rate <= 1.0),
        notice_version TEXT NOT NULL,
        app_session_id TEXT,
        acquisition_id TEXT,
        analytics_user_id TEXT,
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        first_batch_id TEXT NOT NULL REFERENCES ingest_batches(batch_id),
        received_at_utc TEXT NOT NULL,
        CHECK (
            (
                app_session_id IS NOT NULL
                AND acquisition_id IS NULL
                AND analytics_user_id IS NULL
            )
            OR
            (
                app_session_id IS NULL
                AND (acquisition_id IS NOT NULL OR analytics_user_id IS NOT NULL)
            )
        )
    ) STRICT
    """,
    "CREATE INDEX idx_events_occurred ON events(occurred_at_utc)",
    "CREATE INDEX idx_events_received ON events(received_at_utc)",
    "CREATE INDEX idx_events_name_occurred ON events(event_name, occurred_at_utc)",
    """
    CREATE INDEX idx_events_app_version_occurred
    ON events(app_version, occurred_at_utc)
    """,
    """
    CREATE INDEX idx_events_outcome_error_occurred
    ON events(outcome, error_code, occurred_at_utc)
    """,
    """
    CREATE INDEX idx_events_app_session_name
    ON events(app_session_id, event_name)
    """,
    """
    CREATE INDEX idx_events_acquisition_name_occurred
    ON events(acquisition_id, event_name, occurred_at_utc)
    """,
    """
    CREATE INDEX idx_events_analytics_user_name_occurred
    ON events(analytics_user_id, event_name, occurred_at_utc)
    """,
    """
    CREATE UNIQUE INDEX idx_events_client_launch_user_surface_day
    ON events(analytics_user_id, json_extract(payload_json, '$.surface'),
              substr(occurred_at_utc, 1, 10))
    WHERE event_name = 'client_launch'
    """,
)
_EXPECTED_SCHEMA_SQL = {
    ("table", "meta"): _SCHEMA_STATEMENTS[0],
    ("table", "ingest_batches"): _SCHEMA_STATEMENTS[1],
    ("table", "events"): _SCHEMA_STATEMENTS[2],
    ("index", "idx_events_occurred"): _SCHEMA_STATEMENTS[3],
    ("index", "idx_events_received"): _SCHEMA_STATEMENTS[4],
    ("index", "idx_events_name_occurred"): _SCHEMA_STATEMENTS[5],
    ("index", "idx_events_app_version_occurred"): _SCHEMA_STATEMENTS[6],
    ("index", "idx_events_outcome_error_occurred"): _SCHEMA_STATEMENTS[7],
    ("index", "idx_events_app_session_name"): _SCHEMA_STATEMENTS[8],
    ("index", "idx_events_acquisition_name_occurred"): _SCHEMA_STATEMENTS[9],
    ("index", "idx_events_analytics_user_name_occurred"): _SCHEMA_STATEMENTS[10],
    ("index", "idx_events_client_launch_user_surface_day"): _SCHEMA_STATEMENTS[11],
}
_LEGACY_EXPECTED_SCHEMA_SQL = {
    key: value
    for key, value in _EXPECTED_SCHEMA_SQL.items()
    if key != ("index", "idx_events_client_launch_user_surface_day")
}


class StorageError(RuntimeError):
    """Base class for sanitized storage failures."""


class StorageCompatibilityError(StorageError):
    """The database cannot safely be opened by this schema and scope."""

    def __init__(self) -> None:
        super().__init__("telemetry database metadata is incompatible")


class StorageScopeError(StorageError):
    """A caller passed a batch for the other consent scope."""

    def __init__(self) -> None:
        super().__init__("telemetry batch scope does not match storage scope")


class BatchConflictError(StorageError):
    """One batch identifier was reused with different canonical content."""

    def __init__(self) -> None:
        super().__init__("telemetry batch identifier conflict")


class EventConflictError(StorageError):
    """One event identifier was reused with different canonical content."""

    def __init__(self) -> None:
        super().__init__("telemetry event identifier conflict")


@dataclass(frozen=True, slots=True)
class IngestReceipt:
    batch_id: str
    accepted: int
    duplicates: int


@dataclass(frozen=True, slots=True)
class StorageStats:
    batch_count: int
    event_count: int


def _utc_milliseconds(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("storage clock must return timezone-aware UTC")
    normalized = value.astimezone(UTC)
    normalized = normalized.replace(microsecond=(normalized.microsecond // 1000) * 1000)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalized_schema_sql(value: str) -> str:
    return " ".join(value.split())


def _canonical_payload(value: StrictTelemetryModel) -> tuple[str, dict[str, Any], str]:
    raw = canonical_json_bytes(value)
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):  # pragma: no cover - contract invariant
        raise RuntimeError("canonical telemetry payload must be an object")
    return _sha256(raw), decoded, raw.decode("utf-8")


def _client_launch_daily_key(
    payload: dict[str, Any],
) -> tuple[str, str, str] | None:
    if payload.get("event_name") != "client_launch":
        return None
    analytics_user_id = payload.get("analytics_user_id")
    surface = payload.get("surface")
    occurred_at = payload.get("occurred_at_utc")
    if not all(isinstance(value, str) for value in (analytics_user_id, surface, occurred_at)):
        raise StorageCompatibilityError
    return analytics_user_id, surface, occurred_at[:10]


class TelemetryIngestStorage:
    """One open connection locked to a single telemetry consent scope."""

    def __init__(
        self,
        *,
        connection: aiosqlite.Connection,
        scope: ConsentScope,
        database_path: Path,
        protocol_fingerprint: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._connection = connection
        self.scope = scope
        self.database_path = database_path
        self.protocol_fingerprint = protocol_fingerprint
        self._clock = clock
        self._write_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(
        cls,
        database_path: Path,
        scope: ConsentScope,
        *,
        protocol_fingerprint: str = TELEMETRY_PROTOCOL_FINGERPRINT_SHA256,
        clock: Callable[[], datetime] = _default_clock,
    ) -> Self:
        if not isinstance(database_path, Path):
            raise TypeError("database_path must be a Path")
        if not isinstance(scope, ConsentScope):
            raise TypeError("scope must be a ConsentScope")
        if database_path.is_symlink() or database_path.parent.is_symlink():
            raise StorageCompatibilityError

        parent_existed = database_path.parent.exists()
        database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        if not parent_existed and sys.platform != "win32":
            os.chmod(database_path.parent, 0o750)

        try:
            connection = await aiosqlite.connect(database_path)
        except (OSError, sqlite3.Error):
            raise StorageCompatibilityError from None

        try:
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute("PRAGMA busy_timeout = 5000")
            schema_objects = await cls._schema_objects(connection)
            cursor = await connection.execute("PRAGMA user_version")
            user_version_row = await cursor.fetchone()
            await cursor.close()
            if schema_objects or user_version_row != (0,):
                await cls._validate_existing(
                    connection,
                    schema_objects=schema_objects,
                    scope=scope,
                    protocol_fingerprint=protocol_fingerprint,
                )
            else:
                await cls._initialize(
                    connection,
                    scope=scope,
                    protocol_fingerprint=protocol_fingerprint,
                    created_at_utc=_utc_milliseconds(clock()),
                )
            if sys.platform != "win32":
                os.chmod(database_path, 0o640)
            await connection.execute("PRAGMA journal_mode = WAL")
            await connection.execute("PRAGMA synchronous = FULL")
            await connection.commit()
        except (StorageCompatibilityError, ValueError):
            await connection.close()
            raise
        except (OSError, sqlite3.Error):
            await connection.close()
            raise StorageCompatibilityError from None

        return cls(
            connection=connection,
            scope=scope,
            database_path=database_path,
            protocol_fingerprint=protocol_fingerprint,
            clock=clock,
        )

    @staticmethod
    async def _schema_objects(
        connection: aiosqlite.Connection,
    ) -> dict[tuple[str, str], str]:
        cursor = await connection.execute(
            """
            SELECT type, name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' AND type IN ('table', 'index', 'view', 'trigger')
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {(str(row[0]), str(row[1])): "" if row[2] is None else str(row[2]) for row in rows}

    @classmethod
    async def _initialize(
        cls,
        connection: aiosqlite.Connection,
        *,
        scope: ConsentScope,
        protocol_fingerprint: str,
        created_at_utc: str,
    ) -> None:
        try:
            await connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA_STATEMENTS:
                await connection.execute(statement)
            await connection.execute(
                """
                INSERT INTO meta(
                    singleton, schema_version, scope, protocol_fingerprint, created_at_utc
                ) VALUES (1, ?, ?, ?, ?)
                """,
                (SCHEMA_VERSION, scope.value, protocol_fingerprint, created_at_utc),
            )
            await connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise

    @classmethod
    async def _validate_existing(
        cls,
        connection: aiosqlite.Connection,
        *,
        schema_objects: dict[tuple[str, str], str],
        scope: ConsentScope,
        protocol_fingerprint: str,
    ) -> None:
        if (
            protocol_fingerprint == TELEMETRY_PROTOCOL_FINGERPRINT_SHA256
            and schema_objects.keys() == _LEGACY_EXPECTED_SCHEMA_SQL.keys()
        ):
            await cls._migrate_legacy_protocol(
                connection,
                schema_objects=schema_objects,
                scope=scope,
            )
            schema_objects = await cls._schema_objects(connection)
        if schema_objects.keys() != _EXPECTED_SCHEMA_SQL.keys():
            raise StorageCompatibilityError
        for key, expected_sql in _EXPECTED_SCHEMA_SQL.items():
            if _normalized_schema_sql(schema_objects[key]) != _normalized_schema_sql(expected_sql):
                raise StorageCompatibilityError

        cursor = await connection.execute(
            "SELECT schema_version, scope, protocol_fingerprint FROM meta WHERE singleton = 1"
        )
        metadata = await cursor.fetchone()
        await cursor.close()
        cursor = await connection.execute("PRAGMA user_version")
        user_version_row = await cursor.fetchone()
        await cursor.close()
        if user_version_row != (SCHEMA_VERSION,):
            raise StorageCompatibilityError

        for table_name, expected in _EXPECTED_COLUMNS.items():
            cursor = await connection.execute(f"PRAGMA table_info({table_name})")
            columns = tuple(str(row[1]) for row in await cursor.fetchall())
            await cursor.close()
            if columns != expected:
                raise StorageCompatibilityError

        cursor = await connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
        )
        indexes = frozenset(str(row[0]) for row in await cursor.fetchall())
        await cursor.close()
        if indexes != _EXPECTED_INDEXES:
            raise StorageCompatibilityError

        previous_metadata = (
            SCHEMA_VERSION,
            scope.value,
            _PREVIOUS_PROTOCOL_FINGERPRINT_SHA256,
        )
        if (
            protocol_fingerprint == TELEMETRY_PROTOCOL_FINGERPRINT_SHA256
            and metadata == previous_metadata
        ):
            await cls._migrate_compatible_protocol_fingerprint(connection)
            metadata = (SCHEMA_VERSION, scope.value, protocol_fingerprint)
        if metadata != (SCHEMA_VERSION, scope.value, protocol_fingerprint):
            raise StorageCompatibilityError

    @staticmethod
    async def _migrate_compatible_protocol_fingerprint(
        connection: aiosqlite.Connection,
    ) -> None:
        try:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "UPDATE meta SET protocol_fingerprint = ? WHERE singleton = 1",
                (TELEMETRY_PROTOCOL_FINGERPRINT_SHA256,),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise

    @classmethod
    async def _migrate_legacy_protocol(
        cls,
        connection: aiosqlite.Connection,
        *,
        schema_objects: dict[tuple[str, str], str],
        scope: ConsentScope,
    ) -> None:
        for key, expected_sql in _LEGACY_EXPECTED_SCHEMA_SQL.items():
            if _normalized_schema_sql(schema_objects[key]) != _normalized_schema_sql(expected_sql):
                raise StorageCompatibilityError
        cursor = await connection.execute(
            "SELECT schema_version, scope, protocol_fingerprint FROM meta WHERE singleton = 1"
        )
        metadata = await cursor.fetchone()
        await cursor.close()
        if metadata != (
            SCHEMA_VERSION,
            scope.value,
            _LEGACY_PROTOCOL_FINGERPRINT_SHA256,
        ):
            raise StorageCompatibilityError
        cursor = await connection.execute("PRAGMA user_version")
        version = await cursor.fetchone()
        await cursor.close()
        if version != (SCHEMA_VERSION,):
            raise StorageCompatibilityError
        try:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(_SCHEMA_STATEMENTS[11])
            await connection.execute(
                "UPDATE meta SET protocol_fingerprint = ? WHERE singleton = 1",
                (TELEMETRY_PROTOCOL_FINGERPRINT_SHA256,),
            )
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise

    def _require_batch_scope(self, batch: TelemetryBatch) -> None:
        matches = (
            self.scope is ConsentScope.RELIABILITY and isinstance(batch, ReliabilityEventBatch)
        ) or (self.scope is ConsentScope.GROWTH and isinstance(batch, GrowthEventBatch))
        if not matches:
            raise StorageScopeError

    async def ingest(self, batch: TelemetryBatch) -> IngestReceipt:
        if self._closed:
            raise RuntimeError("telemetry storage is closed")
        self._require_batch_scope(batch)
        body_sha256, batch_payload, _batch_json = _canonical_payload(batch)
        batch_id = str(batch.batch_id)
        sent_at_utc = str(batch_payload["sent_at_utc"])
        received_at_utc = _utc_milliseconds(self._clock())

        prepared_events: list[tuple[str, str, dict[str, Any], str]] = []
        for event in batch.events:
            payload_sha256, payload, payload_json = _canonical_payload(event)
            prepared_events.append((str(event.event_id), payload_sha256, payload, payload_json))

        async with self._write_lock:
            await self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._connection.execute(
                    "SELECT body_sha256 FROM ingest_batches WHERE batch_id = ?",
                    (batch_id,),
                )
                existing_batch = await cursor.fetchone()
                await cursor.close()
                if existing_batch is not None:
                    if existing_batch[0] != body_sha256:
                        raise BatchConflictError
                    await self._connection.rollback()
                    return IngestReceipt(
                        batch_id=batch_id,
                        accepted=0,
                        duplicates=len(prepared_events),
                    )

                new_events: list[tuple[str, str, dict[str, Any], str]] = []
                duplicates = 0
                seen_client_launch_keys: set[tuple[str, str, str]] = set()
                for prepared in prepared_events:
                    event_id, payload_sha256, payload, _payload_json = prepared
                    cursor = await self._connection.execute(
                        "SELECT payload_sha256 FROM events WHERE event_id = ?",
                        (event_id,),
                    )
                    existing_event = await cursor.fetchone()
                    await cursor.close()
                    if existing_event is not None and existing_event[0] == payload_sha256:
                        duplicates += 1
                        continue
                    if existing_event is not None:
                        raise EventConflictError
                    launch_key = _client_launch_daily_key(payload)
                    if launch_key is not None:
                        if launch_key in seen_client_launch_keys:
                            duplicates += 1
                            continue
                        cursor = await self._connection.execute(
                            """
                            SELECT 1 FROM events
                            WHERE event_name = 'client_launch'
                              AND analytics_user_id = ?
                              AND json_extract(payload_json, '$.surface') = ?
                              AND substr(occurred_at_utc, 1, 10) = ?
                            LIMIT 1
                            """,
                            launch_key,
                        )
                        existing_launch = await cursor.fetchone()
                        await cursor.close()
                        if existing_launch is not None:
                            duplicates += 1
                            continue
                        seen_client_launch_keys.add(launch_key)
                    new_events.append(prepared)

                accepted = len(new_events)
                await self._connection.execute(
                    """
                    INSERT INTO ingest_batches(
                        batch_id, body_sha256, sent_at_utc, received_at_utc,
                        accepted_count, duplicate_count
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        body_sha256,
                        sent_at_utc,
                        received_at_utc,
                        accepted,
                        duplicates,
                    ),
                )

                for event_id, payload_sha256, payload, payload_json in new_events:
                    await self._insert_event(
                        event_id=event_id,
                        payload_sha256=payload_sha256,
                        payload=payload,
                        payload_json=payload_json,
                        batch_id=batch_id,
                        received_at_utc=received_at_utc,
                    )
                await self._connection.commit()
            except BaseException:
                await self._connection.rollback()
                raise

        return IngestReceipt(
            batch_id=batch_id,
            accepted=accepted,
            duplicates=duplicates,
        )

    async def _insert_event(
        self,
        *,
        event_id: str,
        payload_sha256: str,
        payload: dict[str, Any],
        payload_json: str,
        batch_id: str,
        received_at_utc: str,
    ) -> None:
        await self._connection.execute(
            """
            INSERT INTO events(
                event_id, payload_sha256, event_name, event_version,
                occurred_at_utc, source, app_version, platform, outcome,
                error_code, duration_ms, sample_rate, notice_version,
                app_session_id, acquisition_id, analytics_user_id,
                payload_json, first_batch_id, received_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                payload_sha256,
                payload["event_name"],
                payload["event_version"],
                payload["occurred_at_utc"],
                payload["source"],
                payload["app_version"],
                payload["platform"],
                payload["outcome"],
                payload["error_code"],
                payload["duration_ms"],
                payload["sample_rate"],
                payload["notice_version"],
                payload.get("app_session_id"),
                payload.get("acquisition_id"),
                payload.get("analytics_user_id"),
                payload_json,
                batch_id,
                received_at_utc,
            ),
        )

    async def stats(self) -> StorageStats:
        if self._closed:
            raise RuntimeError("telemetry storage is closed")
        cursor = await self._connection.execute(
            "SELECT (SELECT count(*) FROM ingest_batches), (SELECT count(*) FROM events)"
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:  # pragma: no cover - aggregate query invariant
            raise RuntimeError("telemetry statistics query returned no row")
        return StorageStats(batch_count=int(row[0]), event_count=int(row[1]))

    async def ping(self) -> None:
        if self._closed:
            raise RuntimeError("telemetry storage is closed")
        cursor = await self._connection.execute("SELECT 1")
        await cursor.fetchone()
        await cursor.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._connection.close()


__all__ = [
    "BatchConflictError",
    "EventConflictError",
    "IngestReceipt",
    "SCHEMA_VERSION",
    "StorageCompatibilityError",
    "StorageError",
    "StorageScopeError",
    "StorageStats",
    "TelemetryIngestStorage",
]
