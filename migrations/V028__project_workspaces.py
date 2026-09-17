"""V028 - persistent project workspaces and optional session bindings."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V027__plan_runs"}


CREATE_PROJECT_WORKSPACES = """
CREATE TABLE IF NOT EXISTS project_workspaces (
    workspace_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    path_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    position_at INTEGER NOT NULL,
    pinned_at INTEGER,
    removed_at INTEGER,
    trusted_at INTEGER
)
"""


def _session_columns(conn) -> set[str]:
    return {
        str(row[1])
        for row in conn.cursor().execute("PRAGMA table_info(sessions)").fetchall()
    }


def _session_table_exists(conn) -> bool:
    return (
        conn.cursor()
        .execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
        )
        .fetchone()
        is not None
    )


def apply_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(CREATE_PROJECT_WORKSPACES)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_workspaces_order "
        "ON project_workspaces(removed_at, pinned_at DESC, position_at DESC)"
    )
    if _session_table_exists(conn) and "workspace_id" not in _session_columns(conn):
        cur.execute("ALTER TABLE sessions ADD COLUMN workspace_id TEXT")
    if _session_table_exists(conn):
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_workspace_id "
            "ON sessions(workspace_id)"
        )


def rollback_step(conn) -> None:
    cur = conn.cursor()
    cur.execute("DROP INDEX IF EXISTS idx_sessions_workspace_id")
    if _session_table_exists(conn) and "workspace_id" in _session_columns(conn):
        cur.execute("ALTER TABLE sessions DROP COLUMN workspace_id")
    cur.execute("DROP INDEX IF EXISTS idx_project_workspaces_order")
    cur.execute("DROP TABLE IF EXISTS project_workspaces")


steps = [step(apply_step, rollback_step)]
