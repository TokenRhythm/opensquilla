"""Migration contract for reconnect-safe Goal transcript anchors."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_ID = "V034__goal_message_anchor"


def _migration_version(path: Path) -> int | None:
    prefix = path.name.split("__", 1)[0]
    if prefix.startswith("V") and prefix[1:].isdigit():
        return int(prefix[1:])
    return None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def test_v034_adds_nullable_goal_source_message_anchor(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    migration_slice = tmp_path / "through-v034"
    migration_slice.mkdir()
    for path in MIGRATIONS_DIR.glob("V*.py"):
        version = _migration_version(path)
        if version is not None and version <= 33:
            shutil.copy2(path, migration_slice / path.name)
    apply_pending(str(db_path), migration_slice)

    with sqlite3.connect(db_path) as conn:
        assert "source_user_message_id" not in _columns(conn, "session_goals")

    v034 = MIGRATIONS_DIR / "V034__goal_message_anchor.py"
    shutil.copy2(v034, migration_slice / v034.name)
    assert MIGRATION_ID in apply_pending(str(db_path), migration_slice)

    with sqlite3.connect(db_path) as conn:
        columns = _columns(conn, "session_goals")
        assert "source_user_message_id" in columns
        source_column = next(
            row
            for row in conn.execute("PRAGMA table_info(session_goals)")
            if str(row[1]) == "source_user_message_id"
        )
        assert source_column[3] == 0  # nullable keeps every pre-V034 Goal readable
        assert source_column[4] is None


def test_v034_rollback_and_dependency_are_explicit(tmp_path: Path) -> None:
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
        assert "source_user_message_id" not in _columns(conn, "session_goals")

    files = sorted(path.name for path in MIGRATIONS_DIR.glob("V034__*.py"))
    assert files == ["V034__goal_message_anchor.py"]
    source = (MIGRATIONS_DIR / files[0]).read_text(encoding="utf-8")
    assert "V033__goal_runs" in source
