"""V043 - persist the nullable execution workspace binding for new tasks."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V042__assistant_replay"}


def apply_step(conn) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if columns and "execution_workspace" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN execution_workspace TEXT")


def rollback_step(conn) -> None:
    # SQLite cannot safely drop a column on all supported versions. The
    # nullable field is ignored by older application code, so retain it on an
    # explicit rollback rather than rebuilding the sensitive sessions table.
    pass


steps = [step(apply_step, rollback_step)]
