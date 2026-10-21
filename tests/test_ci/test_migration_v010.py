"""V010 migration apply/rollback + schema invariants."""

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


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table,),
        ).fetchall()
        if not row[0].startswith("sqlite_")
    }


def test_v010_creates_two_tables_and_five_indexes(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert "V010__meta_skill_runs" in applied

    conn = _open_conn(db)
    try:
        tables = _tables(conn)
        assert "meta_skill_runs" in tables
        assert "meta_skill_run_steps" in tables

        run_idx = _indexes(conn, "meta_skill_runs")
        assert {
            "idx_meta_runs_name_started",
            "idx_meta_runs_status_started",
            "idx_meta_runs_session",
            "idx_meta_runs_started",
        }.issubset(run_idx)

        step_idx = _indexes(conn, "meta_skill_run_steps")
        assert "idx_meta_run_steps_status" in step_idx
    finally:
        conn.close()


def test_v010_apply_is_idempotent(tmp_path: Path) -> None:
    """Re-applying V010 over an already-migrated DB is a no-op."""
    db = str(tmp_path / "test.db")
    apply_pending(db, MIGRATIONS_DIR)
    # Re-applying must NOT raise OperationalError
    applied_again = apply_pending(db, MIGRATIONS_DIR)
    assert "V010__meta_skill_runs" not in applied_again
    # Tables still present
    conn = _open_conn(db)
    try:
        tables = _tables(conn)
        assert "meta_skill_runs" in tables
        assert "meta_skill_run_steps" in tables
    finally:
        conn.close()


def test_v010_run_status_check_constraint(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    apply_pending(db, MIGRATIONS_DIR)
    conn = _open_conn(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO meta_skill_runs ("
                "run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
                "triggered_by, status, started_at_ms, inputs_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("r1", "m", "d", "{}", "soft_meta_invoke", "BOGUS", 0, "{}"),
            )
    finally:
        conn.close()


def test_v010_step_status_check_constraint(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    apply_pending(db, MIGRATIONS_DIR)
    conn = _open_conn(db)
    try:
        conn.execute(
            "INSERT INTO meta_skill_runs ("
            "run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            "triggered_by, status, started_at_ms, inputs_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r1", "m", "d", "{}", "soft_meta_invoke", "ok", 0, "{}"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO meta_skill_run_steps ("
                "run_id, step_id, step_kind, declared_skill, effective_skill, "
                "status, started_at_ms, rendered_inputs_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("r1", "s1", "agent", "x", "x", "BOGUS", 0, "{}"),
            )
    finally:
        conn.close()


def test_v010_cascade_requires_pragma_foreign_keys(tmp_path: Path) -> None:
    """C2: CASCADE only works when PRAGMA foreign_keys=ON. Document the contract."""
    db = str(tmp_path / "test.db")
    apply_pending(db, MIGRATIONS_DIR)

    # WITHOUT PRAGMA - child rows persist
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO meta_skill_runs ("
            "run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            "triggered_by, status, started_at_ms, inputs_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r1", "m", "d", "{}", "soft_meta_invoke", "ok", 0, "{}"),
        )
        conn.execute(
            "INSERT INTO meta_skill_run_steps ("
            "run_id, step_id, step_kind, declared_skill, effective_skill, "
            "status, started_at_ms, rendered_inputs_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r1", "s1", "agent", "x", "x", "ok", 0, "{}"),
        )
        conn.execute("DELETE FROM meta_skill_runs WHERE run_id='r1'")
        conn.commit()
        leftover = conn.execute(
            "SELECT COUNT(*) FROM meta_skill_run_steps WHERE run_id='r1'"
        ).fetchone()[0]
        assert leftover == 1, "Without PRAGMA, child rows MUST persist (orphans)"
    finally:
        conn.close()

    # WITH PRAGMA - child rows cascade
    conn = _open_conn(db)
    try:
        conn.execute(
            "INSERT INTO meta_skill_runs ("
            "run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            "triggered_by, status, started_at_ms, inputs_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r2", "m", "d", "{}", "soft_meta_invoke", "ok", 0, "{}"),
        )
        conn.execute(
            "INSERT INTO meta_skill_run_steps ("
            "run_id, step_id, step_kind, declared_skill, effective_skill, "
            "status, started_at_ms, rendered_inputs_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r2", "s1", "agent", "x", "x", "ok", 0, "{}"),
        )
        conn.execute("DELETE FROM meta_skill_runs WHERE run_id='r2'")
        conn.commit()
        leftover = conn.execute(
            "SELECT COUNT(*) FROM meta_skill_run_steps WHERE run_id='r2'"
        ).fetchone()[0]
        assert leftover == 0, "With PRAGMA, child rows MUST cascade"
    finally:
        conn.close()
