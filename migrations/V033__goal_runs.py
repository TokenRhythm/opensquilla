"""V033 - generation-fenced current Goals and command receipts."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V032__meta_launch_discard_tombstones"}

CREATE_SESSION_GOALS = """
CREATE TABLE IF NOT EXISTS session_goals (
    session_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    session_epoch INTEGER NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    goal_id TEXT NOT NULL UNIQUE,
    objective TEXT NOT NULL CHECK (length(objective) BETWEEN 1 AND 4000),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'blocked', 'usage_limited', 'complete')),
    state_revision INTEGER NOT NULL DEFAULT 1 CHECK (state_revision >= 1),
    objective_revision INTEGER NOT NULL DEFAULT 1 CHECK (objective_revision >= 1),
    progress_revision INTEGER NOT NULL DEFAULT 0 CHECK (progress_revision >= 0),
    progress_json TEXT,
    continuation_seq INTEGER NOT NULL DEFAULT 0 CHECK (continuation_seq >= 0),
    active_task_id TEXT,
    terminal_task_id TEXT,
    turns_started INTEGER NOT NULL DEFAULT 0 CHECK (turns_started >= 0),
    turns_settled INTEGER NOT NULL DEFAULT 0 CHECK (turns_settled >= 0),
    window_turns_started INTEGER NOT NULL DEFAULT 0 CHECK (window_turns_started >= 0),
    active_time_ms INTEGER NOT NULL DEFAULT 0 CHECK (active_time_ms >= 0),
    window_active_time_ms INTEGER NOT NULL DEFAULT 0 CHECK (window_active_time_ms >= 0),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    reasoning_tokens INTEGER NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    cache_read_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    pause_reason TEXT,
    blocked_reason TEXT,
    terminal_reason TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1),
    FOREIGN KEY (session_key) REFERENCES sessions(session_key) ON DELETE CASCADE
)
"""

CREATE_GOAL_COMMAND_RECEIPTS = """
CREATE TABLE IF NOT EXISTS goal_command_receipts (
    receipt_id TEXT PRIMARY KEY,
    source_scope TEXT NOT NULL,
    request_session_key TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    action TEXT NOT NULL
        CHECK (action IN ('set', 'edit', 'pause', 'resume', 'clear')),
    request_fingerprint TEXT NOT NULL,
    accepted_session_id TEXT NOT NULL,
    accepted_session_epoch INTEGER NOT NULL DEFAULT 0
        CHECK (accepted_session_epoch >= 0),
    response_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    FOREIGN KEY (request_session_key) REFERENCES sessions(session_key) ON DELETE CASCADE
)
"""

CREATE_INDEXES: tuple[str, ...] = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_session_goals_active_task
    ON session_goals(active_task_id)
    WHERE active_task_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_session_goals_status
    ON session_goals(status, updated_at_ms)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_goal_command_receipts_request
    ON goal_command_receipts(source_scope, request_session_key, client_request_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_goal_command_receipts_session
    ON goal_command_receipts(request_session_key, created_at_ms)
    """,
)


def apply_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(CREATE_SESSION_GOALS)
    cur.execute(CREATE_GOAL_COMMAND_RECEIPTS)
    for statement in CREATE_INDEXES:
        cur.execute(statement)


def rollback_step(conn) -> None:
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS goal_command_receipts")
    cur.execute("DROP TABLE IF EXISTS session_goals")


steps = [step(apply_step, rollback_step)]
