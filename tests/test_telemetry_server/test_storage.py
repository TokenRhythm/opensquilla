from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from opensquilla.telemetry.contracts import (
    TELEMETRY_PROTOCOL_FINGERPRINT_SHA256,
    GrowthEventBatch,
    ReliabilityEventBatch,
    TelemetryWireTarget,
    parse_telemetry_wire,
)
from opensquilla.telemetry.contracts.common import ConsentScope
from opensquilla.telemetry.server import storage as storage_module
from opensquilla.telemetry.server.storage import (
    BatchConflictError,
    EventConflictError,
    StorageCompatibilityError,
    TelemetryIngestStorage,
)

_EVENT_ID = "00000000-0000-4000-8000-000000000001"
_SECOND_EVENT_ID = "00000000-0000-4000-8000-000000000002"
_SESSION_ID = "00000000-0000-4000-8000-000000000003"
_BATCH_ID = "00000000-0000-4000-8000-000000000004"
_SECOND_BATCH_ID = "00000000-0000-4000-8000-000000000005"
_ACQUISITION_ID = "00000000-0000-4000-8000-000000000006"
_ANALYTICS_ID = "00000000-0000-4000-8000-000000000007"


def _reliability_event(*, event_id: str = _EVENT_ID, duration_ms: int = 120) -> dict[str, object]:
    return {
        "event_name": "app_start_result",
        "event_version": 1,
        "event_id": event_id,
        "occurred_at_utc": "2026-09-01T01:02:03.456Z",
        "source": "desktop",
        "app_version": "1.2.3",
        "platform": "macos",
        "outcome": "success",
        "error_code": None,
        "duration_ms": duration_ms,
        "consent_scope": "reliability",
        "notice_version": "reliability-v1",
        "sample_rate": 1.0,
        "app_session_id": _SESSION_ID,
        "failure_stage": None,
    }


def _growth_event(*, event_id: str = _EVENT_ID) -> dict[str, object]:
    return {
        "event_name": "landing_view",
        "event_version": 1,
        "event_id": event_id,
        "occurred_at_utc": "2026-09-01T01:02:03.456Z",
        "source": "website",
        "app_version": None,
        "platform": "macos",
        "outcome": None,
        "error_code": None,
        "duration_ms": None,
        "consent_scope": "growth",
        "notice_version": "growth-v1",
        "sample_rate": 1,
        "acquisition_id": _ACQUISITION_ID,
    }


def _client_launch(
    *, event_id: str, occurred_at: str = "2026-09-01T01:02:03.456Z"
) -> dict[str, object]:
    return {
        "event_name": "client_launch",
        "event_version": 1,
        "event_id": event_id,
        "occurred_at_utc": occurred_at,
        "source": "gateway",
        "app_version": "1.2.3",
        "platform": "macos",
        "outcome": None,
        "error_code": None,
        "duration_ms": None,
        "consent_scope": "growth",
        "notice_version": "growth-v1",
        "sample_rate": 1,
        "analytics_user_id": _ANALYTICS_ID,
        "surface": "tui",
        "entrypoint": "chat",
        "execution_mode": "gateway",
    }


def _batch_dict(
    *,
    scope: ConsentScope,
    batch_id: str = _BATCH_ID,
    events: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if events is None:
        events = [_reliability_event() if scope is ConsentScope.RELIABILITY else _growth_event()]
    return {
        "batch_version": 1,
        "batch_id": batch_id,
        "sent_at_utc": "2026-09-01T01:03:00.000Z",
        "events": events,
    }


def _parse_batch(
    payload: dict[str, object], *, scope: ConsentScope
) -> ReliabilityEventBatch | GrowthEventBatch:
    target = (
        TelemetryWireTarget.RELIABILITY_BATCH
        if scope is ConsentScope.RELIABILITY
        else TelemetryWireTarget.GROWTH_BATCH
    )
    return parse_telemetry_wire(
        json.dumps(payload, separators=(",", ":")).encode(),
        target=target,
    )


def _sqlite_tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def _sqlite_schema_objects(path: Path) -> set[tuple[str, str]]:
    with sqlite3.connect(path) as connection:
        return {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }


@pytest.mark.asyncio
async def test_initializes_locked_schema_and_persists_strict_rows(tmp_path: Path) -> None:
    database = tmp_path / "reliability" / "events.sqlite3"
    storage = await TelemetryIngestStorage.open(database, ConsentScope.RELIABILITY)
    try:
        batch = _parse_batch(
            _batch_dict(
                scope=ConsentScope.RELIABILITY,
                events=[
                    _reliability_event(),
                    _reliability_event(event_id=_SECOND_EVENT_ID),
                ],
            ),
            scope=ConsentScope.RELIABILITY,
        )
        receipt = await storage.ingest(batch)
        stats = await storage.stats()
    finally:
        await storage.close()

    assert receipt.accepted == 2
    assert receipt.duplicates == 0
    assert stats.batch_count == 1
    assert stats.event_count == 2
    assert _sqlite_tables(database) >= {"meta", "ingest_batches", "events"}

    with sqlite3.connect(database) as connection:
        metadata = connection.execute(
            "SELECT schema_version, scope, protocol_fingerprint FROM meta"
        ).fetchone()
        row = connection.execute(
            """
            SELECT event_name, event_version, app_session_id,
                   acquisition_id, analytics_user_id, payload_sha256
            FROM events ORDER BY event_id LIMIT 1
            """
        ).fetchone()
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            )
        }

    assert metadata == (
        1,
        "reliability",
        TELEMETRY_PROTOCOL_FINGERPRINT_SHA256,
    )
    assert row[:5] == (
        "app_start_result",
        1,
        _SESSION_ID,
        None,
        None,
    )
    assert len(row[5]) == 64
    assert user_version == 1
    assert indexes >= {
        "idx_events_app_version_occurred",
        "idx_events_outcome_error_occurred",
        "idx_events_app_session_name",
        "idx_events_acquisition_name_occurred",
        "idx_events_analytics_user_name_occurred",
    }

    if sys.platform != "win32":
        assert stat.S_IMODE(database.parent.stat().st_mode) == 0o750
        assert stat.S_IMODE(database.stat().st_mode) == 0o640


@pytest.mark.asyncio
async def test_initialization_failure_rolls_back_every_schema_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "events.sqlite3"
    monkeypatch.setattr(
        storage_module,
        "_SCHEMA_STATEMENTS",
        (storage_module._SCHEMA_STATEMENTS[0], "THIS IS NOT VALID SQL"),
    )

    with pytest.raises(StorageCompatibilityError):
        await TelemetryIngestStorage.open(database, ConsentScope.RELIABILITY)

    assert _sqlite_tables(database) == set()


@pytest.mark.asyncio
async def test_rejects_scope_or_fingerprint_mismatch_without_writing(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite3"
    original = await TelemetryIngestStorage.open(database, ConsentScope.RELIABILITY)
    await original.close()
    before = database.read_bytes()

    with pytest.raises(StorageCompatibilityError):
        await TelemetryIngestStorage.open(database, ConsentScope.GROWTH)
    assert database.read_bytes() == before

    with pytest.raises(StorageCompatibilityError):
        await TelemetryIngestStorage.open(
            database,
            ConsentScope.RELIABILITY,
            protocol_fingerprint="0" * 64,
        )
    assert database.read_bytes() == before


@pytest.mark.asyncio
async def test_rejects_same_named_but_altered_schema(tmp_path: Path) -> None:
    database = tmp_path / "events.sqlite3"
    original = await TelemetryIngestStorage.open(database, ConsentScope.RELIABILITY)
    await original.close()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX idx_events_occurred")
        connection.execute("CREATE INDEX idx_events_occurred ON events(received_at_utc)")
        connection.commit()

    with pytest.raises(StorageCompatibilityError):
        await TelemetryIngestStorage.open(database, ConsentScope.RELIABILITY)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "foreign_sql",
    [
        "CREATE TABLE legacy_only (value TEXT NOT NULL)",
        "CREATE VIEW legacy_only AS SELECT 'value' AS value",
    ],
)
async def test_rejects_nonempty_foreign_database_without_adding_tables(
    tmp_path: Path, foreign_sql: str
) -> None:
    database = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(foreign_sql)
        connection.commit()
    before_objects = _sqlite_schema_objects(database)

    with pytest.raises(StorageCompatibilityError):
        await TelemetryIngestStorage.open(database, ConsentScope.RELIABILITY)

    assert _sqlite_schema_objects(database) == before_objects


@pytest.mark.asyncio
async def test_rejects_foreign_user_version_without_initializing(tmp_path: Path) -> None:
    database = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 99")
        connection.commit()

    with pytest.raises(StorageCompatibilityError):
        await TelemetryIngestStorage.open(database, ConsentScope.RELIABILITY)

    assert _sqlite_schema_objects(database) == set()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


@pytest.mark.skipif(sys.platform == "win32", reason="symlink semantics differ on Windows")
@pytest.mark.asyncio
async def test_rejects_database_symlink_without_opening_its_target(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    target.write_bytes(b"not-a-telemetry-database")
    database = tmp_path / "events.sqlite3"
    os.symlink(target, database)

    with pytest.raises(StorageCompatibilityError):
        await TelemetryIngestStorage.open(database, ConsentScope.RELIABILITY)

    assert target.read_bytes() == b"not-a-telemetry-database"


@pytest.mark.asyncio
async def test_retries_are_idempotent_for_batch_and_event_ids(tmp_path: Path) -> None:
    storage = await TelemetryIngestStorage.open(
        tmp_path / "events.sqlite3", ConsentScope.RELIABILITY
    )
    try:
        payload = _batch_dict(scope=ConsentScope.RELIABILITY)
        batch = _parse_batch(payload, scope=ConsentScope.RELIABILITY)
        first = await storage.ingest(batch)
        exact_retry = await storage.ingest(batch)

        different_batch = deepcopy(payload)
        different_batch["batch_id"] = _SECOND_BATCH_ID
        event_retry = await storage.ingest(
            _parse_batch(different_batch, scope=ConsentScope.RELIABILITY)
        )
        stats = await storage.stats()
    finally:
        await storage.close()

    assert (first.accepted, first.duplicates) == (1, 0)
    assert (exact_retry.accepted, exact_retry.duplicates) == (0, 1)
    assert (event_retry.accepted, event_retry.duplicates) == (0, 1)
    assert (stats.batch_count, stats.event_count) == (2, 1)


@pytest.mark.asyncio
async def test_batch_hash_conflict_rolls_back(tmp_path: Path) -> None:
    storage = await TelemetryIngestStorage.open(
        tmp_path / "events.sqlite3", ConsentScope.RELIABILITY
    )
    try:
        payload = _batch_dict(scope=ConsentScope.RELIABILITY)
        await storage.ingest(_parse_batch(payload, scope=ConsentScope.RELIABILITY))

        changed = deepcopy(payload)
        changed["events"][0]["duration_ms"] = 121  # type: ignore[index]
        with pytest.raises(BatchConflictError):
            await storage.ingest(_parse_batch(changed, scope=ConsentScope.RELIABILITY))
        stats = await storage.stats()
    finally:
        await storage.close()

    assert (stats.batch_count, stats.event_count) == (1, 1)


@pytest.mark.asyncio
async def test_event_hash_conflict_rolls_back_whole_new_batch(tmp_path: Path) -> None:
    storage = await TelemetryIngestStorage.open(
        tmp_path / "events.sqlite3", ConsentScope.RELIABILITY
    )
    try:
        first_payload = _batch_dict(scope=ConsentScope.RELIABILITY)
        await storage.ingest(_parse_batch(first_payload, scope=ConsentScope.RELIABILITY))

        conflicting_payload = _batch_dict(
            scope=ConsentScope.RELIABILITY,
            batch_id=_SECOND_BATCH_ID,
            events=[
                _reliability_event(event_id=_SECOND_EVENT_ID),
                _reliability_event(duration_ms=999),
            ],
        )
        with pytest.raises(EventConflictError):
            await storage.ingest(_parse_batch(conflicting_payload, scope=ConsentScope.RELIABILITY))
        stats = await storage.stats()
    finally:
        await storage.close()

    assert (stats.batch_count, stats.event_count) == (1, 1)


@pytest.mark.asyncio
async def test_reliability_and_growth_databases_are_independent(tmp_path: Path) -> None:
    reliability_path = tmp_path / "reliability.sqlite3"
    growth_path = tmp_path / "growth.sqlite3"
    reliability = await TelemetryIngestStorage.open(reliability_path, ConsentScope.RELIABILITY)
    growth = await TelemetryIngestStorage.open(growth_path, ConsentScope.GROWTH)
    try:
        reliability_receipt = await reliability.ingest(
            _parse_batch(
                _batch_dict(scope=ConsentScope.RELIABILITY),
                scope=ConsentScope.RELIABILITY,
            )
        )
        growth_receipt = await growth.ingest(
            _parse_batch(
                _batch_dict(scope=ConsentScope.GROWTH),
                scope=ConsentScope.GROWTH,
            )
        )
        reliability_stats = await reliability.stats()
        growth_stats = await growth.stats()
    finally:
        await reliability.close()
        await growth.close()

    assert (reliability_receipt.accepted, reliability_receipt.duplicates) == (1, 0)
    assert (growth_receipt.accepted, growth_receipt.duplicates) == (1, 0)
    assert reliability_stats.event_count == 1
    assert growth_stats.event_count == 1
    assert reliability_path.read_bytes() != growth_path.read_bytes()


@pytest.mark.asyncio
async def test_client_launch_semantic_daily_deduplication(tmp_path: Path) -> None:
    storage = await TelemetryIngestStorage.open(tmp_path / "growth.sqlite3", ConsentScope.GROWTH)
    try:
        batch = _parse_batch(
            _batch_dict(
                scope=ConsentScope.GROWTH,
                events=[
                    _client_launch(event_id=_EVENT_ID),
                    _client_launch(event_id=_SECOND_EVENT_ID),
                ],
            ),
            scope=ConsentScope.GROWTH,
        )
        receipt = await storage.ingest(batch)
        stats = await storage.stats()
    finally:
        await storage.close()

    assert (receipt.accepted, receipt.duplicates) == (1, 1)
    assert stats.event_count == 1


@pytest.mark.asyncio
async def test_exact_previous_protocol_database_is_migrated_in_place(tmp_path: Path) -> None:
    database = tmp_path / "legacy-growth.sqlite3"
    with sqlite3.connect(database) as connection:
        for statement in storage_module._SCHEMA_STATEMENTS[:-1]:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO meta(singleton, schema_version, scope, protocol_fingerprint,
                             created_at_utc)
            VALUES (1, 1, 'growth', ?, '2026-09-01T00:00:00.000Z')
            """,
            (storage_module._LEGACY_PROTOCOL_FINGERPRINT_SHA256,),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    storage = await TelemetryIngestStorage.open(database, ConsentScope.GROWTH)
    await storage.close()

    with sqlite3.connect(database) as connection:
        fingerprint = connection.execute(
            "SELECT protocol_fingerprint FROM meta WHERE singleton = 1"
        ).fetchone()[0]
        index = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND name = 'idx_events_client_launch_user_surface_day'
            """
        ).fetchone()
    assert fingerprint == TELEMETRY_PROTOCOL_FINGERPRINT_SHA256
    assert index == ("idx_events_client_launch_user_surface_day",)


@pytest.mark.asyncio
async def test_compatible_previous_fingerprint_is_advanced_in_place(tmp_path: Path) -> None:
    database = tmp_path / "previous-growth.sqlite3"
    storage = await TelemetryIngestStorage.open(database, ConsentScope.GROWTH)
    await storage.close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE meta SET protocol_fingerprint = ? WHERE singleton = 1",
            (storage_module._PREVIOUS_PROTOCOL_FINGERPRINT_SHA256,),
        )

    reopened = await TelemetryIngestStorage.open(database, ConsentScope.GROWTH)
    await reopened.close()

    with sqlite3.connect(database) as connection:
        fingerprint = connection.execute(
            "SELECT protocol_fingerprint FROM meta WHERE singleton = 1"
        ).fetchone()[0]
    assert fingerprint == TELEMETRY_PROTOCOL_FINGERPRINT_SHA256
