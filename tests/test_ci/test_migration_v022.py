"""V022 migration: content-free daily telemetry aggregates."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='telemetry_daily_usage'"
    ).fetchone()
    return row is not None


def test_v022_creates_daily_usage_table(tmp_path: Path) -> None:
    db = str(tmp_path / "v022.db")
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert "V022__telemetry_daily_usage" in applied

    conn = sqlite3.connect(db)
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(telemetry_daily_usage)").fetchall()
        }
        assert columns == {
            "day",
            "conversation_turns",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "cache_write_tokens",
            "updated_at",
            "uploaded_at",
        }
        conn.execute(
            """
            INSERT INTO telemetry_daily_usage (day, updated_at)
            VALUES ('2026-07-19', 1)
            """
        )
        row = conn.execute(
            """
            SELECT conversation_turns, input_tokens, output_tokens,
                   cached_tokens, cache_write_tokens, uploaded_at
            FROM telemetry_daily_usage
            """
        ).fetchone()
        assert row == (0, 0, 0, 0, 0, None)
    finally:
        conn.close()


def test_v022_rollback_drops_daily_usage_table(tmp_path: Path) -> None:
    db = str(tmp_path / "v022-rollback.db")
    apply_pending(db, MIGRATIONS_DIR)
    backend = get_backend(f"sqlite:///{db}")
    migrations = read_migrations(str(MIGRATIONS_DIR))
    by_id = {migration.id: migration for migration in migrations}
    backend.rollback_migrations([by_id["V022__telemetry_daily_usage"]])

    conn = sqlite3.connect(db)
    try:
        assert not _table_exists(conn)
    finally:
        conn.close()
