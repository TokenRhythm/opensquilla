"""Migration contract for generation-fenced session Goals."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_ID = "V033__goal_runs"


def _migration_version(path: Path) -> int | None:
    prefix = path.name.split("__", 1)[0]
    if prefix.startswith("V") and prefix[1:].isdigit():
        return int(prefix[1:])
    return None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def test_v033_creates_current_goal_and_command_receipt_contract(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    migration_slice = tmp_path / "through-v033"
    migration_slice.mkdir()
    for path in MIGRATIONS_DIR.glob("V*.py"):
        version = _migration_version(path)
        if version is not None and version <= 32:
            shutil.copy2(path, migration_slice / path.name)
    apply_pending(str(db_path), migration_slice)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (session_key TEXT PRIMARY KEY)")

    v033 = MIGRATIONS_DIR / "V033__goal_runs.py"
    shutil.copy2(v033, migration_slice / v033.name)
    assert MIGRATION_ID in apply_pending(str(db_path), migration_slice)

    with sqlite3.connect(db_path) as conn:
        assert _columns(conn, "session_goals") == {
            "session_key",
            "session_id",
            "session_epoch",
            "goal_id",
            "objective",
            "status",
            "state_revision",
            "objective_revision",
            "progress_revision",
            "progress_json",
            "continuation_seq",
            "active_task_id",
            "terminal_task_id",
            "turns_started",
            "turns_settled",
            "window_turns_started",
            "active_time_ms",
            "window_active_time_ms",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_tokens",
            "pause_reason",
            "blocked_reason",
            "terminal_reason",
            "created_at_ms",
            "updated_at_ms",
            "finished_at_ms",
            "schema_version",
        }
        assert _columns(conn, "goal_command_receipts") == {
            "receipt_id",
            "source_scope",
            "request_session_key",
            "client_request_id",
            "action",
            "request_fingerprint",
            "accepted_session_id",
            "accepted_session_epoch",
            "response_json",
            "created_at_ms",
        }
        indexes = {
            str(row[1]): bool(row[2])
            for row in conn.execute("PRAGMA index_list(session_goals)")
        }
        assert indexes["idx_session_goals_active_task"] is True
        receipt_indexes = {
            str(row[1]): bool(row[2])
            for row in conn.execute("PRAGMA index_list(goal_command_receipts)")
        }
        assert receipt_indexes["uq_goal_command_receipts_request"] is True
        no_goal_lookup_plan = [
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM session_goals WHERE session_key = ?",
                ("agent:main:webchat:no-goal",),
            )
        ]
        assert len(no_goal_lookup_plan) == 1
        assert "SEARCH session_goals" in no_goal_lookup_plan[0]
        assert "session_key=?" in no_goal_lookup_plan[0]
        assert "SCAN session_goals" not in no_goal_lookup_plan[0]
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "goal_runs" not in tables


def test_v033_foreign_keys_delete_goal_and_receipts_with_session(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    migration_slice = tmp_path / "through-v033"
    migration_slice.mkdir()
    for path in MIGRATIONS_DIR.glob("V*.py"):
        version = _migration_version(path)
        if version is not None and version <= 32:
            shutil.copy2(path, migration_slice / path.name)
    apply_pending(str(db_path), migration_slice)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (session_key TEXT PRIMARY KEY)")
    shutil.copy2(
        MIGRATIONS_DIR / "V033__goal_runs.py",
        migration_slice / "V033__goal_runs.py",
    )
    apply_pending(str(db_path), migration_slice)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("INSERT INTO sessions VALUES ('agent:main:webchat:test')")
        conn.execute(
            """
            INSERT INTO session_goals (
                session_key, session_id, goal_id, objective,
                created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("agent:main:webchat:test", "session-1", "goal-1", "Ship it", 1, 1),
        )
        conn.execute(
            """
            INSERT INTO goal_command_receipts (
                receipt_id, source_scope, request_session_key,
                client_request_id, action, request_fingerprint,
                accepted_session_id, response_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "receipt-1",
                "gateway:goals",
                "agent:main:webchat:test",
                "00000000-0000-4000-8000-000000000000",
                "set",
                "sha256:test",
                "session-1",
                "{}",
                1,
            ),
        )
        conn.execute("DELETE FROM sessions WHERE session_key = ?", ("agent:main:webchat:test",))
        assert conn.execute("SELECT COUNT(*) FROM session_goals").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM goal_command_receipts").fetchone() == (
            0,
        )


def test_v033_rollback_and_dependency_are_explicit(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    apply_pending(str(db_path), MIGRATIONS_DIR)
    backend = get_backend("sqlite:///" + str(db_path))
    try:
        migration = read_migrations(str(MIGRATIONS_DIR)).filter(
            lambda item: item.id == MIGRATION_ID
        )
        with backend.lock():
            backend.rollback_migrations(migration)
    finally:
        backend.connection.close()
    with sqlite3.connect(db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "session_goals" not in tables
        assert "goal_command_receipts" not in tables

    files = sorted(path.name for path in MIGRATIONS_DIR.glob("V033__*.py"))
    assert files == ["V033__goal_runs.py"]
    source = (MIGRATIONS_DIR / files[0]).read_text(encoding="utf-8")
    assert "V032__meta_launch_discard_tombstones" in source
    assert not list(MIGRATIONS_DIR.glob("V031__goal_run_retry.py"))
