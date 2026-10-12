"""V034 - durable origin-message anchor for current Goals."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V033__goal_runs"}

TABLE = "session_goals"
COLUMN = "source_user_message_id"


def _columns(conn) -> set[str]:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({TABLE})")
    return {str(row[1]) for row in cur.fetchall()}


def apply_step(conn) -> None:
    if COLUMN not in _columns(conn):
        conn.cursor().execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} TEXT")


def rollback_step(conn) -> None:
    if COLUMN in _columns(conn):
        conn.cursor().execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")


steps = [step(apply_step, rollback_step)]
