"""Upgrade semantics for the per-session routing columns."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import ModuleType

from yoyo import read_migrations

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_ID = "V036__session_model_routing"


def _migration_module() -> ModuleType:
    migration = next(
        item for item in read_migrations(str(MIGRATIONS_DIR)) if item.id == MIGRATION_ID
    )
    migration.load()
    return migration.module


def test_v036_preserves_legacy_null_as_lazy_initialization_marker() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE sessions (session_key TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO sessions (session_key) VALUES ('legacy')")

        module = _migration_module()
        module.apply_step(connection)
        # Mixed boot/recovery paths may run the guarded helper more than once.
        module.apply_step(connection)

        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(sessions)")
        }
        row = connection.execute(
            "SELECT model_routing_mode, model_routing_revision FROM sessions"
        ).fetchone()
        assert "model_routing_mode" in columns
        assert columns["model_routing_revision"][3] == 1
        assert row == (None, 0)
    finally:
        connection.close()
