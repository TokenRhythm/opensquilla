"""V036 - durable per-session model-routing selection."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V035__pending_chat_inputs"}

TABLE = "sessions"
COLUMNS: tuple[tuple[str, str], ...] = (
    ("model_routing_mode", "TEXT"),
    ("model_routing_revision", "INTEGER NOT NULL DEFAULT 0"),
)


def _table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def _table_columns(conn, table: str) -> set[str]:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return {str(row[1]) for row in cur.fetchall()}


def apply_step(conn) -> None:
    if not _table_exists(conn, TABLE):
        return
    columns = _table_columns(conn, TABLE)
    cur = conn.cursor()
    for name, definition in COLUMNS:
        if name not in columns:
            cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {definition}")
    # NULL model_routing_mode deliberately remains the legacy marker: the
    # first session read atomically snapshots the then-current global setting.
    cur.execute(
        "UPDATE sessions SET model_routing_revision = 0 "
        "WHERE model_routing_revision IS NULL OR model_routing_revision < 0"
    )


def rollback_step(conn) -> None:
    if not _table_exists(conn, TABLE):
        return
    columns = _table_columns(conn, TABLE)
    cur = conn.cursor()
    for name, _definition in reversed(COLUMNS):
        if name in columns:
            cur.execute(f"ALTER TABLE {TABLE} DROP COLUMN {name}")


steps = [step(apply_step, rollback_step)]
