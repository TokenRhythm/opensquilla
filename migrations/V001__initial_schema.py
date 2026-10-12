"""V001 - baseline schema: ensure schema_version column on session tables.

For a fresh database this is a near no-op (SessionStorage.connect() creates
the tables with the schema_version column already present). For a database
carried over from pre-S-MIGRATE, this adds the column in place.

Rollback drops the column (SQLite 3.35+ supports DROP COLUMN natively).
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = set()


TABLES = ("sessions", "transcript_entries", "session_summaries")
COLUMN_NAME = "schema_version"
COLUMN_DDL = f"{COLUMN_NAME} INTEGER NOT NULL DEFAULT 1"


def _has_column(conn, table: str, column: str) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def _table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def apply_step(conn) -> None:
    cur = conn.cursor()
    for table in TABLES:
        if not _table_exists(conn, table):
            # Fresh DB - SessionStorage.connect() will create the table with
            # schema_version already in its CREATE TABLE statement.
            continue
        if _has_column(conn, table, COLUMN_NAME):
            continue
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {COLUMN_DDL}")


def rollback_step(conn) -> None:
    cur = conn.cursor()
    for table in TABLES:
        if not _table_exists(conn, table):
            continue
        if not _has_column(conn, table, COLUMN_NAME):
            continue
        cur.execute(f"ALTER TABLE {table} DROP COLUMN {COLUMN_NAME}")


steps = [step(apply_step, rollback_step)]
