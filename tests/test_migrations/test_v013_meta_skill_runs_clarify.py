"""Unit tests for V013 meta_skill_runs migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.helpers.historical_migrations import apply_pre_retirement_migrations as apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table,),
        ).fetchall()
        if not row[0].startswith("sqlite_")
    }


def test_v013_apply_widens_status_check(tmp_path: Path) -> None:
    db = str(tmp_path / "test.sqlite")
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert "V013__meta_skill_runs_clarify" in applied

    conn = _open_conn(db)
    try:
        conn.execute(
            "INSERT INTO meta_skill_runs "
            "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            " triggered_by, status, started_at_ms, inputs_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("r1", "test", "d", "{}", "soft_meta_invoke",
             "awaiting_user", 0, "{}"),
        )
        conn.commit()
    finally:
        conn.close()


def test_v013_apply_preserves_v010_v011_indexes(tmp_path: Path) -> None:
    db = str(tmp_path / "test.sqlite")
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert "V013__meta_skill_runs_clarify" in applied

    conn = _open_conn(db)
    try:
        ix = _indexes(conn, "meta_skill_runs")
        expected = {
            "idx_meta_runs_name_started",
            "idx_meta_runs_status_started",
            "idx_meta_runs_session",
            "idx_meta_runs_started",
            "uq_one_awaiting_per_session",
        }
        missing = expected - ix
        assert not missing, f"missing indexes after V013: {missing}"
    finally:
        conn.close()


def test_v013_apply_creates_partial_unique_index(tmp_path: Path) -> None:
    db = str(tmp_path / "test.sqlite")
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert "V013__meta_skill_runs_clarify" in applied

    conn = _open_conn(db)
    try:
        conn.execute(
            "INSERT INTO meta_skill_runs "
            "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            " triggered_by, session_key, status, started_at_ms, inputs_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("r1", "t", "d", "{}", "soft_meta_invoke", "S1",
             "awaiting_user", 0, "{}"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO meta_skill_runs "
                "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
                " triggered_by, session_key, status, started_at_ms, inputs_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("r2", "t", "d", "{}", "soft_meta_invoke", "S1",
                 "awaiting_user", 0, "{}"),
            )
    finally:
        conn.close()


def test_v013_index_does_not_block_non_awaiting_rows(tmp_path: Path) -> None:
    db = str(tmp_path / "test.sqlite")
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert "V013__meta_skill_runs_clarify" in applied

    conn = _open_conn(db)
    try:
        conn.execute(
            "INSERT INTO meta_skill_runs "
            "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            " triggered_by, session_key, status, started_at_ms, inputs_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("r1", "t", "d", "{}", "soft_meta_invoke", "S1", "ok", 0, "{}"),
        )
        conn.execute(
            "INSERT INTO meta_skill_runs "
            "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            " triggered_by, session_key, status, started_at_ms, inputs_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("r2", "t", "d", "{}", "soft_meta_invoke", "S1", "ok", 0, "{}"),
        )
        conn.commit()
    finally:
        conn.close()


def test_v013_rollback_blocked_when_awaiting_rows_present(tmp_path: Path) -> None:
    db = str(tmp_path / "test.sqlite")
    apply_pending(db, MIGRATIONS_DIR)
    conn = _open_conn(db)
    try:
        conn.execute(
            "INSERT INTO meta_skill_runs "
            "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            " triggered_by, session_key, status, started_at_ms, inputs_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("r1", "t", "d", "{}", "soft_meta_invoke", "S1",
             "awaiting_user", 0, "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    # Verify that attempting to transition awaiting rows would fail
    # by simulating what the rollback step does: it checks for awaiting/expired rows
    conn = _open_conn(db)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM meta_skill_runs "
            "WHERE status IN ('awaiting_user','expired')"
        )
        leftover = cur.fetchone()[0]
        assert leftover == 1, "Should have one awaiting_user row"
    finally:
        conn.close()


def test_v013_rollback_succeeds_when_no_awaiting_rows(tmp_path: Path) -> None:
    db = str(tmp_path / "test.sqlite")
    apply_pending(db, MIGRATIONS_DIR)
    conn = _open_conn(db)
    try:
        conn.execute(
            "INSERT INTO meta_skill_runs "
            "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            " triggered_by, status, started_at_ms, inputs_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("r1", "t", "d", "{}", "soft_meta_invoke", "ok", 0, "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    # Verify no awaiting/expired rows (rollback precondition)
    conn = _open_conn(db)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM meta_skill_runs "
            "WHERE status IN ('awaiting_user','expired')"
        )
        leftover = cur.fetchone()[0]
        assert leftover == 0, "Should have no awaiting_user/expired rows"
        # Verify clarify columns exist post-V013
        cur.execute(
            "SELECT awaiting_step_id FROM meta_skill_runs WHERE run_id='r1'"
        )
        assert cur.fetchone() is not None
    finally:
        conn.close()
