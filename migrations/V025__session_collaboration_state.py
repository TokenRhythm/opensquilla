"""V025 - durable per-session collaboration mode and active plan pointer."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V024__usage_native_billing_receipts"}

TABLE = "sessions"
COLUMNS: tuple[tuple[str, str], ...] = (
    ("collaboration_mode", "TEXT NOT NULL DEFAULT 'default'"),
    ("collaboration_revision", "INTEGER NOT NULL DEFAULT 0"),
    ("active_plan_revision_id", "TEXT"),
)


def _table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
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
    cur = conn.cursor()
    existing = _table_columns(conn, TABLE)
    for column, ddl in COLUMNS:
        if column not in existing:
            cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN {column} {ddl}")
    cur.execute(
        """
        UPDATE sessions
        SET collaboration_mode = 'default'
        WHERE collaboration_mode IS NULL
           OR collaboration_mode NOT IN ('default', 'plan')
        """
    )
    cur.execute(
        """
        UPDATE sessions
        SET collaboration_revision = 0
        WHERE collaboration_revision IS NULL OR collaboration_revision < 0
        """
    )


def rollback_step(conn) -> None:
    if not _table_exists(conn, TABLE):
        return
    cur = conn.cursor()
    existing = _table_columns(conn, TABLE)
    for column, _ddl in reversed(COLUMNS):
        if column in existing:
            cur.execute(f"ALTER TABLE {TABLE} DROP COLUMN {column}")


steps = [step(apply_step, rollback_step)]
