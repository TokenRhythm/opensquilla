"""Regression tests for the schema-only V021 usage ledger migration."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import (
    SchemaAheadError,
    apply_pending,
    assert_schema_not_ahead,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
V021_ID = "V021__usage_ledger"

TABLES = {
    "usage_events",
    "usage_event_items",
    "usage_ledger_state",
    "usage_legacy_baselines",
}

INDEXES = {
    "idx_usage_events_completed",
    "idx_usage_events_session_completed",
    "idx_usage_events_agent_completed",
    "idx_usage_events_status_completed",
    "idx_usage_events_status_started",
    "idx_usage_event_items_model",
    "idx_usage_event_items_provider",
    "idx_usage_legacy_baselines_captured",
}


def _is_before_v021(path: Path) -> bool:
    """Return whether a versioned migration belongs to the pre-V021 history."""
    prefix = path.name.split("__", 1)[0]
    return prefix.startswith("V") and prefix[1:].isdigit() and int(prefix[1:]) < 21


def _objects(conn: sqlite3.Connection, object_type: str) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = ?", (object_type,)
        ).fetchall()
    }


def test_v021_is_schema_only_and_enforces_event_idempotency(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    migration_slice = tmp_path / "through_v021"
    migration_slice.mkdir()
    for path in MIGRATIONS_DIR.glob("V*.py"):
        if _is_before_v021(path):
            shutil.copy2(path, migration_slice / path.name)
    apply_pending(str(db_path), migration_slice)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE sessions (
                session_key TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO sessions VALUES ('agent:main:test', 'session-1', 7)"
        )
        conn.execute(
            """
            CREATE TABLE transcript_entries (
                session_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                role TEXT NOT NULL,
                turn_usage TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE compacted_transcript_entries (
                session_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                role TEXT NOT NULL,
                turn_usage TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()
        tables_before = _objects(conn, "table")
        indexes_before = _objects(conn, "index")
    finally:
        conn.close()

    # Exercise V021 in isolation. Applying the repository's entire migration
    # history here would make every later schema addition look like it belonged
    # to V021 and force this regression test to change for each new release.
    v021_path = MIGRATIONS_DIR / "V021__usage_ledger.py"
    shutil.copy2(v021_path, migration_slice / v021_path.name)
    applied = apply_pending(str(db_path), migration_slice)
    assert V021_ID in applied

    conn = sqlite3.connect(db_path)
    try:
        assert _objects(conn, "table") - tables_before == TABLES
        created_indexes = _objects(conn, "index") - indexes_before
        assert {
            name for name in created_indexes if not name.startswith("sqlite_autoindex_")
        } == INDEXES
        assert {
            "idx_transcript_usage_backfill",
            "idx_compacted_usage_backfill",
            "idx_sessions_id_key",
        }.isdisjoint(_objects(conn, "index"))
        assert conn.execute("SELECT COUNT(*) FROM usage_ledger_state").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM usage_legacy_baselines").fetchone() == (
            0,
        )
        assert conn.execute(
            "SELECT session_key, session_id, input_tokens FROM sessions"
        ).fetchall() == [("agent:main:test", "session-1", 7)]

        values = (
            "event-1",
            "execution-1",
            0,
            "session-1",
            1_700_000_000_000,
            "live_provider",
        )
        conn.execute(
            """
            INSERT INTO usage_events (
                event_id, execution_id, call_index, session_id, started_at_ms, origin
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO usage_events (
                    event_id, execution_id, call_index, session_id,
                    started_at_ms, origin
                ) VALUES ('event-2', 'execution-1', 0, 'session-1', 1, 'live_provider')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE usage_events SET cost_nanos = 2 WHERE event_id = 'event-1'"
            )
    finally:
        conn.close()


def test_v021_rollback_and_schema_ahead_guard(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    apply_pending(str(db_path), MIGRATIONS_DIR)

    backend = get_backend("sqlite:///" + str(db_path))
    try:
        migration = read_migrations(str(MIGRATIONS_DIR)).filter(
            lambda item: item.id == V021_ID
        )
        with backend.lock():
            backend.rollback_migrations(migration)
    finally:
        backend.connection.close()

    conn = sqlite3.connect(db_path)
    try:
        assert TABLES.isdisjoint(_objects(conn, "table"))
    finally:
        conn.close()

    apply_pending(str(db_path), MIGRATIONS_DIR)
    older_build = tmp_path / "migrations_before_v021"
    older_build.mkdir()
    for path in MIGRATIONS_DIR.glob("V*.py"):
        if _is_before_v021(path):
            shutil.copy2(path, older_build / path.name)
    with pytest.raises(SchemaAheadError, match=V021_ID):
        assert_schema_not_ahead(str(db_path), older_build)


def test_v021_prefix_and_dependency_are_unique() -> None:
    files = sorted(path.name for path in MIGRATIONS_DIR.glob("V021__*.py"))
    assert files == ["V021__usage_ledger.py"]
    source = (MIGRATIONS_DIR / files[0]).read_text(encoding="utf-8")
    assert "V020__turn_ingress_receipts" in source
