"""V027 - server-authoritative mutable plan execution runs."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V026__plan_revisions"}

CREATE_PLAN_RUNS = """
CREATE TABLE IF NOT EXISTS plan_runs (
    run_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    session_epoch INTEGER NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    plan_revision_id TEXT NOT NULL,
    supersedes_run_id TEXT,
    driver_kind TEXT NOT NULL DEFAULT 'manual'
        CHECK (driver_kind IN ('manual', 'goal')),
    driver_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (
            status IN (
                'queued', 'running', 'paused', 'blocked',
                'completed', 'cancelled', 'superseded'
            )
        ),
    step_states TEXT NOT NULL,
    current_step_id TEXT,
    state_revision INTEGER NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
    active_task_id TEXT,
    pause_reason TEXT,
    terminal_reason TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
)
"""

CREATE_INDEXES: tuple[str, ...] = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_runs_active_session
    ON plan_runs(session_key)
    WHERE status IN ('queued', 'running', 'paused', 'blocked')
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_plan_runs_session_history
    ON plan_runs(session_key, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_plan_runs_revision
    ON plan_runs(plan_revision_id, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_plan_runs_driver
    ON plan_runs(driver_id)
    WHERE driver_id IS NOT NULL
    """,
)


def apply_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(CREATE_PLAN_RUNS)
    for statement in CREATE_INDEXES:
        cur.execute(statement)


def rollback_step(conn) -> None:
    conn.cursor().execute("DROP TABLE IF EXISTS plan_runs")


steps = [step(apply_step, rollback_step)]
