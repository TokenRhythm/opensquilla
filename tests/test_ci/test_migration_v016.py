"""V016 migration: allow manual_command meta run provenance."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from tests.helpers.historical_migrations import apply_pre_retirement_migrations as apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
V016_ID = "V016__meta_skill_runs_triggered_by_manual_command"


def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _insert_run(conn: sqlite3.Connection, run_id: str, triggered_by: str) -> None:
    conn.execute(
        """
        INSERT INTO meta_skill_runs (
            run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json,
            triggered_by, status, started_at_ms, inputs_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            "synth",
            "deadbeef",
            "{}",
            triggered_by,
            "ok",
            1_000_000,
            "{}",
        ),
    )
    conn.commit()


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table,),
        ).fetchall()
        if not row[0].startswith("sqlite_")
    }


def test_v016_relaxes_triggered_by_check_to_accept_manual_command(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "v016.db")
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert V016_ID in applied

    conn = _open_conn(db)
    try:
        _insert_run(conn, "r1", "hard_takeover")
        _insert_run(conn, "r2", "soft_meta_invoke")
        _insert_run(conn, "r3", "auto_cron")
        _insert_run(conn, "r4", "auto_dream")
        _insert_run(conn, "r5", "manual_command")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(conn, "r6", "made_up_value")
        rows = conn.execute(
            "SELECT triggered_by FROM meta_skill_runs ORDER BY run_id"
        ).fetchall()
        assert [r[0] for r in rows] == [
            "hard_takeover",
            "soft_meta_invoke",
            "auto_cron",
            "auto_dream",
            "manual_command",
        ]
    finally:
        conn.close()


def test_v016_migrates_existing_rows_before_accepting_manual_command(
    tmp_path: Path,
) -> None:
    db = str(tmp_path / "v016_existing.db")
    apply_pending(db, MIGRATIONS_DIR)
    backend = get_backend(f"sqlite:///{db}")
    migrations = read_migrations(str(MIGRATIONS_DIR))
    by_id = {migration.id: migration for migration in migrations}
    backend.rollback_migrations([by_id[V016_ID]])

    conn = _open_conn(db)
    try:
        _insert_run(conn, "r1", "hard_takeover")
        _insert_run(conn, "r2", "soft_meta_invoke")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(conn, "r3", "manual_command")
        conn.rollback()
    finally:
        conn.close()

    applied = apply_pending(db, MIGRATIONS_DIR)
    assert V016_ID in applied

    conn = _open_conn(db)
    try:
        rows = conn.execute(
            "SELECT run_id, triggered_by FROM meta_skill_runs ORDER BY run_id"
        ).fetchall()
        assert rows == [
            ("r1", "hard_takeover"),
            ("r2", "soft_meta_invoke"),
        ]
        _insert_run(conn, "r3", "manual_command")
    finally:
        conn.close()


def test_v016_preserves_indexes_and_awaiting_unique_index(tmp_path: Path) -> None:
    db = str(tmp_path / "v016_idx.db")
    apply_pending(db, MIGRATIONS_DIR)
    conn = _open_conn(db)
    try:
        ix = _indexes(conn, "meta_skill_runs")
        assert "idx_meta_runs_name_started" in ix
        assert "idx_meta_runs_status_started" in ix
        assert "idx_meta_runs_session" in ix
        assert "idx_meta_runs_started" in ix
        assert "uq_one_awaiting_per_session" in ix
    finally:
        conn.close()
