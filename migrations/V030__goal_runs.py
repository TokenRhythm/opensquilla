"""V030 - server-authoritative long-running goal execution runs."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V028__project_workspaces"}

CREATE_GOAL_RUNS = """
CREATE TABLE IF NOT EXISTS goal_runs (
    goal_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    goal_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'paused', 'complete', 'blocked', 'cancelled')),
    progress TEXT,
    turns INTEGER NOT NULL DEFAULT 0,
    idle_turns INTEGER NOT NULL DEFAULT 0,
    blocked_reason TEXT,
    blocked_retries INTEGER NOT NULL DEFAULT 0,
    plan_run_id TEXT,
    started_at INTEGER NOT NULL,
    last_turn_at INTEGER,
    finished_at INTEGER,
    terminal_reason TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
)
"""

CREATE_INDEXES: tuple[str, ...] = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_goal_runs_active
    ON goal_runs (session_key)
    WHERE status IN ('running', 'paused')
    """,
)


def apply_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(CREATE_GOAL_RUNS)
    for statement in CREATE_INDEXES:
        cur.execute(statement)


def rollback_step(conn) -> None:
    conn.cursor().execute("DROP TABLE IF EXISTS goal_runs")


steps = [step(apply_step, rollback_step)]
