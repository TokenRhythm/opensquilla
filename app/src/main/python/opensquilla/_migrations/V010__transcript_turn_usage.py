"""V010 - persist per-turn usage metadata on transcript entries."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V009__transcript_reasoning_content"}

TABLE = "transcript_entries"
COLUMN = "turn_usage"
ADD_COLUMN_DDL = f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} TEXT"
DROP_COLUMN_DDL = f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}"


def _table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def _has_column(conn, table: str, column: str) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _sqlite_version(conn) -> tuple[int, int, int]:
    cur = conn.cursor()
    cur.execute("SELECT sqlite_version()")
    raw = cur.fetchone()[0]
    parts = raw.split(".")
    while len(parts) < 3:
        parts.append("0")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def apply_step(conn) -> None:
    if not _table_exists(conn, TABLE):
        return
    if _has_column(conn, TABLE, COLUMN):
        return
    cur = conn.cursor()
    cur.execute(ADD_COLUMN_DDL)


def rollback_step(conn) -> None:
    if not _table_exists(conn, TABLE):
        return
    if not _has_column(conn, TABLE, COLUMN):
        return
    if _sqlite_version(conn) < (3, 35, 0):
        return
    cur = conn.cursor()
    cur.execute(DROP_COLUMN_DDL)


steps = [step(apply_step, rollback_step)]
