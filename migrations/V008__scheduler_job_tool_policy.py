"""V008 - scheduler job tool policy (wrong-database no-op).

Adds a persisted per-job tool policy column for cron jobs.

``scheduler_jobs`` lives in ``scheduler.db`` (``JobStore`` opened from
``gateway/boot.py``), NOT in ``sessions.db`` — and ``apply_pending`` only
ever runs against ``sessions.db``. On real split-database deployments the
table is absent from the migration connection and this step no-ops,
recorded in the sessions.db ledger without touching scheduler data. The
real in-place upgrade is ``JobStore._migrate``'s connect-time ADD COLUMN
pass (``scheduler/persistence.py``); the guards below keep this step
harmless where the table does share a file with the session store (e.g.
ad-hoc test setups).
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V007__session_cost_source_rollup"}

TABLE = "scheduler_jobs"
COLUMN = "tool_policy_json"
DDL = "TEXT NOT NULL DEFAULT '{}'"


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
    if not _has_column(conn, TABLE, COLUMN):
        conn.cursor().execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} {DDL}")


def rollback_step(conn) -> None:
    if not _table_exists(conn, TABLE):
        return
    if _has_column(conn, TABLE, COLUMN):
        conn.cursor().execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")


steps = [step(apply_step, rollback_step)]
