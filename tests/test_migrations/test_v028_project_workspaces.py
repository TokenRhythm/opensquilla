from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_ID = "V028__project_workspaces"


def _migration_version(path: Path) -> int | None:
    prefix = path.name.split("__", 1)[0]
    if prefix.startswith("V") and prefix[1:].isdigit():
        return int(prefix[1:])
    return None


def test_v028_adds_workspace_table_and_session_binding(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    migration_slice = tmp_path / "through_v028"
    migration_slice.mkdir()
    for path in MIGRATIONS_DIR.glob("V*.py"):
        version = _migration_version(path)
        if version is not None and version <= 27:
            shutil.copy2(path, migration_slice / path.name)
    apply_pending(str(db_path), migration_slice)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE sessions (session_key TEXT PRIMARY KEY, title TEXT)"
        )

    v028_path = MIGRATIONS_DIR / "V028__project_workspaces.py"
    shutil.copy2(v028_path, migration_slice / v028_path.name)
    assert MIGRATION_ID in apply_pending(str(db_path), migration_slice)

    with sqlite3.connect(db_path) as conn:
        workspace_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(project_workspaces)").fetchall()
        }
        session_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        assert {
            "workspace_id",
            "path",
            "path_key",
            "display_name",
            "position_at",
            "pinned_at",
            "removed_at",
            "trusted_at",
        } <= workspace_columns
        assert "workspace_id" in session_columns

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
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        session_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        assert "project_workspaces" not in tables
        assert "workspace_id" not in session_columns


def test_v028_prefix_and_dependency_are_unique() -> None:
    files = sorted(path.name for path in MIGRATIONS_DIR.glob("V028__*.py"))
    assert files == ["V028__project_workspaces.py"]
    source = (MIGRATIONS_DIR / files[0]).read_text(encoding="utf-8")
    assert "V027__plan_runs" in source
