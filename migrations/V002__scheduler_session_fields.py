"""V002 - scheduler session-forward fields (wrong-database no-op).

Adds ``origin_session_key`` to ``scheduler_jobs`` so the scheduler can
carry the scheduling session identity through to delivery-time fan-out.

``scheduler_jobs`` lives in ``scheduler.db`` (``JobStore`` opened from
``gateway/boot.py``), NOT in ``sessions.db`` — and ``apply_pending`` only
ever runs against ``sessions.db``. On real split-database deployments the
table is therefore absent from the migration connection and this step
no-ops, recorded in the sessions.db ledger without touching scheduler
data. The real in-place upgrade is ``JobStore._migrate``'s connect-time
ADD COLUMN pass (``scheduler/persistence.py``).

The guards below keep the step harmless and idempotent on databases
where ``scheduler_jobs`` does share a file with the session store (e.g.
ad-hoc test setups). Rollback drops the column (SQLite 3.35+ supports
DROP COLUMN natively).
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V001__initial_schema"}


TABLE = "scheduler_jobs"
COLUMN = "origin_session_key"
COLUMN_DDL = f"{COLUMN} TEXT NOT NULL DEFAULT ''"


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
        # Fresh DB - JobStore.open() creates the table with the in-process
        # ADD COLUMN loop handling origin_session_key itself.
        return
    if _has_column(conn, TABLE, COLUMN):
        return
    cur = conn.cursor()
    cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN_DDL}")


def rollback_step(conn) -> None:
    if not _table_exists(conn, TABLE):
        return
    if not _has_column(conn, TABLE, COLUMN):
        return
    cur = conn.cursor()
    cur.execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")


steps = [step(apply_step, rollback_step)]
