"""V014 migration: allow user_input rows in meta_skill_run_steps."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from tests.helpers.historical_migrations import apply_pre_retirement_migrations as apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _insert_run(conn: sqlite3.Connection, run_id: str = "r1") -> None:
    conn.execute(
        """
        INSERT INTO meta_skill_runs (
            run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json,
            triggered_by, status, started_at_ms, inputs_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, "meta-travel-planner", "deadbeef", "{}",
            "soft_meta_invoke", "running", 1_000_000, "{}",
        ),
    )
    conn.commit()


def _insert_step(conn: sqlite3.Connection, kind: str, step_id: str = "s1") -> None:
    conn.execute(
        """
        INSERT INTO meta_skill_run_steps (
            run_id, step_id, step_kind, declared_skill, effective_skill,
            status, started_at_ms, rendered_inputs_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "r1", step_id, kind, "trip_collect", "trip_collect",
            "running", 1_000_001, "{}",
        ),
    )
    conn.commit()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_v014_accepts_user_input_step_kind(tmp_path: Path) -> None:
    db = str(tmp_path / "v014.db")
    applied = apply_pending(db, MIGRATIONS_DIR)
    assert "V014__meta_skill_run_steps_allow_user_input" in applied

    conn = _open_conn(db)
    try:
        _insert_run(conn)
        _insert_step(conn, "user_input")
        row = conn.execute(
            "SELECT step_kind FROM meta_skill_run_steps WHERE step_id = 's1'"
        ).fetchone()
        assert row == ("user_input",)
    finally:
        conn.close()


def test_v014_keeps_prior_step_kinds_and_rejects_unknown(tmp_path: Path) -> None:
    db = str(tmp_path / "v014_existing.db")
    apply_pending(db, MIGRATIONS_DIR)

    conn = _open_conn(db)
    try:
        _insert_run(conn)
        for idx, kind in enumerate(
            ("agent", "llm_classify", "llm_chat", "tool_call", "skill_exec")
        ):
            _insert_step(conn, kind, step_id=f"s{idx}")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_step(conn, "made_up", step_id="bad")
    finally:
        conn.close()


def test_v015_rollback_then_v014_rollback_recreates_step_table(tmp_path: Path) -> None:
    db = str(tmp_path / "v015_v014_rollback.db")
    apply_pending(db, MIGRATIONS_DIR)

    conn = _open_conn(db)
    try:
        _insert_run(conn)
        _insert_step(conn, "agent")
        assert "usage_json" in _column_names(conn, "meta_skill_run_steps")
    finally:
        conn.close()

    backend = get_backend(f"sqlite:///{db}")
    migrations = read_migrations(str(MIGRATIONS_DIR))
    by_id = {migration.id: migration for migration in migrations}
    backend.rollback_migrations([by_id["V015__meta_skill_step_usage"]])

    conn = _open_conn(db)
    try:
        assert "usage_json" not in _column_names(conn, "meta_skill_run_steps")
    finally:
        conn.close()

    backend.rollback_migrations([
        by_id["V014__meta_skill_run_steps_allow_user_input"]
    ])

    conn = _open_conn(db)
    try:
        row = conn.execute(
            "SELECT step_id, step_kind FROM meta_skill_run_steps WHERE step_id='s1'"
        ).fetchone()
        assert row == ("s1", "agent")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_step(conn, "user_input", step_id="after-rollback")
    finally:
        conn.close()


def test_v014_rollback_blocks_when_user_input_step_rows_exist(tmp_path: Path) -> None:
    db = str(tmp_path / "v014_user_input_rollback.db")
    apply_pending(db, MIGRATIONS_DIR)

    conn = _open_conn(db)
    try:
        _insert_run(conn)
        _insert_step(conn, "user_input")
    finally:
        conn.close()

    backend = get_backend(f"sqlite:///{db}")
    migrations = read_migrations(str(MIGRATIONS_DIR))
    by_id = {migration.id: migration for migration in migrations}
    backend.rollback_migrations([by_id["V015__meta_skill_step_usage"]])

    with pytest.raises(RuntimeError, match="user_input"):
        backend.rollback_migrations([
            by_id["V014__meta_skill_run_steps_allow_user_input"]
        ])
