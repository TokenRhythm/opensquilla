"""V009 - persist assistant reasoning content for DeepSeek replay.

DeepSeek thinking mode requires prior assistant ``reasoning_content`` to be
passed back on later turns. The transcript table therefore needs an optional
text column so the runtime can preserve that provider-specific replay state.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V008__scheduler_job_tool_policy"}

TABLE = "transcript_entries"
COLUMN = "reasoning_content"
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
