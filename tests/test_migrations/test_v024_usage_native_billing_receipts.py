"""Upgrade and rollback coverage for provider-native billing receipts."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_ID = "V024__usage_native_billing_receipts"
TABLES = {
    "usage_item_billing_receipts",
    "usage_billing_receipt_state",
}


def _migration_version(path: Path) -> int | None:
    prefix = path.name.split("__", 1)[0]
    if prefix.startswith("V") and prefix[1:].isdigit():
        return int(prefix[1:])
    return None


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _insert_historical_usage_item(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """
        INSERT INTO usage_events (
            event_id, execution_id, call_index, session_id, started_at_ms,
            completed_at_ms, status, cost_source, coverage_status, origin
        ) VALUES ('event-1', 'execution-1', 0, 'session-1', 100, 200,
                  'finalized', 'provider_billed', 'complete', 'live_provider')
        """
    )
    conn.execute(
        """
        INSERT INTO usage_event_items (
            event_id, ordinal, cost_source
        ) VALUES ('event-1', 0, 'provider_billed')
        """
    )
    conn.commit()


def test_v024_marks_cutover_without_backfilling_historical_items(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    migration_slice = tmp_path / "through_v024"
    migration_slice.mkdir()
    for path in MIGRATIONS_DIR.glob("V*.py"):
        version = _migration_version(path)
        if version is not None and version <= 23:
            shutil.copy2(path, migration_slice / path.name)
    apply_pending(str(db_path), migration_slice)

    with sqlite3.connect(db_path) as conn:
        _insert_historical_usage_item(conn)

    v024_path = MIGRATIONS_DIR / "V024__usage_native_billing_receipts.py"
    shutil.copy2(v024_path, migration_slice / v024_path.name)
    applied = apply_pending(str(db_path), migration_slice)
    assert MIGRATION_ID in applied

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert TABLES <= _tables(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM usage_item_billing_receipts"
        ).fetchone() == (0,)
        state = conn.execute(
            "SELECT tracking_started_at_ms, schema_version "
            "FROM usage_billing_receipt_state WHERE singleton_id = 1"
        ).fetchone()
        assert state is not None
        assert state[0] >= 0
        assert state[1] == 1

        conn.execute(
            """
            INSERT INTO usage_item_billing_receipts (
                event_id, ordinal, currency, status, amount_nanos,
                usd_equivalent_nanos, fx_native_per_usd_nanos
            ) VALUES ('event-1', 0, 'CNY', 'confirmed', 6975, 1000, 6975000000)
            """
        )
        conn.commit()

    backend = get_backend("sqlite:///" + str(db_path))
    try:
        migration = read_migrations(str(migration_slice)).filter(
            lambda item: item.id == MIGRATION_ID
        )
        with backend.lock():
            backend.rollback_migrations(migration)
    finally:
        backend.connection.close()

    with sqlite3.connect(db_path) as conn:
        assert TABLES.isdisjoint(_tables(conn))
        assert conn.execute(
            "SELECT event_id, ordinal, cost_source FROM usage_event_items"
        ).fetchall() == [("event-1", 0, "provider_billed")]
        assert conn.execute(
            "SELECT billed_cost_nanos FROM usage_events WHERE event_id = 'event-1'"
        ).fetchone() == (0,)


def test_v024_prefix_and_dependency_are_unique() -> None:
    files = sorted(path.name for path in MIGRATIONS_DIR.glob("V024__*.py"))
    assert files == ["V024__usage_native_billing_receipts.py"]
    source = (MIGRATIONS_DIR / files[0]).read_text(encoding="utf-8")
    assert "V023__router_deployment_telemetry" in source
