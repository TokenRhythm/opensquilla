"""Nullable replay upgrade preserves both active and archived legacy rows."""

import runpy
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def migration():
    path = Path(__file__).resolve().parents[2] / "migrations" / "V041__assistant_replay.py"
    with patch("yoyo.step"):
        return runpy.run_path(str(path))


def test_additive_migration_is_idempotent_and_does_not_backfill(migration):
    with sqlite3.connect(":memory:") as conn:
        for table in migration["TABLES"]:
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, content TEXT)")
            conn.execute(f"INSERT INTO {table} (content) VALUES (?)", ("legacy synthetic",))
        migration["apply_step"](conn)
        migration["apply_step"](conn)
        for table in migration["TABLES"]:
            columns = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
            assert columns["assistant_replay"][3:5] == (0, None)
            assert conn.execute(f"SELECT content, assistant_replay FROM {table}").fetchall() == [
                ("legacy synthetic", None)
            ]
            conn.execute(f"UPDATE {table} SET assistant_replay = ?", ('{"version":1}',))
        migration["rollback_step"](conn)
        for table in migration["TABLES"]:
            assert conn.execute(f"SELECT assistant_replay FROM {table}").fetchone() == (
                '{"version":1}',
            )


def test_additive_migration_tolerates_uncreated_bootstrap_tables(migration):
    with sqlite3.connect(":memory:") as conn:
        migration["apply_step"](conn)
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
