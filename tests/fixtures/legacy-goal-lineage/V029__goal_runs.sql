-- Goal-specific DDL extracted from public commit 2a2ec4d0e64f73b7fdd1707bfeed7bfd8cab2560
-- Source: migrations/V029__goal_runs.py. Data below are synthetic only.

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
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_goal_runs_active
    ON goal_runs (session_key)
    WHERE status IN ('running', 'paused');

INSERT INTO goal_runs (
    goal_id, session_key, agent_id, goal_text, status, progress, turns,
    idle_turns, blocked_reason, blocked_retries, plan_run_id,
    started_at, last_turn_at, finished_at, terminal_reason, created_at, updated_at
) VALUES (
    'synthetic-goal-running', 'agent:main:webchat:synthetic-running', 'main',
    'Verify synthetic pending work.', 'running', '{"completed":["inspect"]}', 3,
    1, NULL, 0, 'synthetic-plan-running',
    1000, 3000, NULL, NULL, 1000, 3000
), (
    'synthetic-goal-paused', 'agent:main:webchat:synthetic-paused', 'main',
    'Preserve a synthetic paused objective.', 'paused', '{"completed":["read"]}', 5,
    2, 'Synthetic earlier blocker.', 2, 'synthetic-plan-paused',
    1000, 5000, NULL, NULL, 1000, 5000
), (
    'synthetic-goal-complete', 'agent:main:webchat:synthetic-running', 'main',
    'An earlier synthetic completed objective.', 'complete', 'Synthetic completion.', 2,
    0, NULL, 0, NULL,
    100, 900, 950, 'complete', 100, 950
);
