"""V007 - session cost source rollup fields."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V006__scheduler_reservations"}

TABLE = "sessions"
COLUMNS = [
    ("total_cost_usd", "REAL NOT NULL DEFAULT 0.0"),
    ("billed_cost_usd", "REAL NOT NULL DEFAULT 0.0"),
    ("estimated_cost_component_usd", "REAL NOT NULL DEFAULT 0.0"),
    ("cost_source", "TEXT NOT NULL DEFAULT 'none'"),
    ("missing_cost_entries", "INTEGER NOT NULL DEFAULT 0"),
]


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


def apply_step(conn) -> None:
    if not _table_exists(conn, TABLE):
        return
    cur = conn.cursor()
    for column, ddl in COLUMNS:
        if not _has_column(conn, TABLE, column):
            cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN {column} {ddl}")
    cur.execute(
        """
        UPDATE sessions
        SET
            total_cost_usd = COALESCE(total_cost_usd, 0.0) + COALESCE(estimated_cost_usd, 0.0),
            estimated_cost_component_usd = COALESCE(estimated_cost_component_usd, 0.0)
                + COALESCE(estimated_cost_usd, 0.0),
            cost_source = CASE
                WHEN COALESCE(estimated_cost_usd, 0.0) > 0.0 THEN 'opensquilla_estimate'
                ELSE COALESCE(cost_source, 'none')
            END
        WHERE COALESCE(total_cost_usd, 0.0) = 0.0
          AND COALESCE(billed_cost_usd, 0.0) = 0.0
          AND COALESCE(estimated_cost_component_usd, 0.0) = 0.0
          AND COALESCE(missing_cost_entries, 0) = 0
          AND COALESCE(estimated_cost_usd, 0.0) > 0.0
        """
    )


def rollback_step(conn) -> None:
    if not _table_exists(conn, TABLE):
        return
    cur = conn.cursor()
    for column, _ddl in reversed(COLUMNS):
        if _has_column(conn, TABLE, column):
            cur.execute(f"ALTER TABLE {TABLE} DROP COLUMN {column}")


steps = [step(apply_step, rollback_step)]
