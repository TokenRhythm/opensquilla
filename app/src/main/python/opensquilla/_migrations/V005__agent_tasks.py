"""V005 - agent task runtime ledger.

Creates the ``agent_tasks`` table used by the server-side task runtime to
persist task status, source attribution, queue mode, timestamps, terminal
reason, and error classification. The in-process queue is intentionally not
persisted here; this table is the durable ledger used for status/list/restart
recovery.

Rollback drops the ledger table and indexes. Task rows are operational state,
not user transcript content.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V004__memory_schema_version"}


TABLE = "agent_tasks"
CREATE_DDL = f"""
CREATE TABLE {TABLE} (
    task_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT 'main',
    source_kind TEXT NOT NULL,
    queue_mode TEXT NOT NULL,
    run_kind TEXT NOT NULL DEFAULT 'default',
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    terminal_reason TEXT,
    error_class TEXT,
    error_message TEXT,
    details TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1
)
"""
INDEX_SESSION_STATUS = "idx_agent_tasks_session_status"
INDEX_STATUS_UPDATED = "idx_agent_tasks_status_updated"


def _table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def apply_step(conn) -> None:
    if _table_exists(conn, TABLE):
        return
    cur = conn.cursor()
    cur.execute(CREATE_DDL)
    cur.execute(
        f"CREATE INDEX {INDEX_SESSION_STATUS} ON {TABLE}(session_key, status)"
    )
    cur.execute(
        f"CREATE INDEX {INDEX_STATUS_UPDATED} ON {TABLE}(status, updated_at)"
    )


def rollback_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(f"DROP INDEX IF EXISTS {INDEX_STATUS_UPDATED}")
    cur.execute(f"DROP INDEX IF EXISTS {INDEX_SESSION_STATUS}")
    cur.execute(f"DROP TABLE IF EXISTS {TABLE}")


steps = [step(apply_step, rollback_step)]
