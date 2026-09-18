-- Goal-specific DDL extracted from public commit 896be9be24622285124de4983a3196b820fe47a7
-- Source: migrations/V030__goal_run_retry.py. Data below are synthetic only.
-- Apply after V029__goal_runs.sql; this is the exact historical migration ID.

ALTER TABLE goal_runs ADD COLUMN failure_retries INTEGER NOT NULL DEFAULT 0;

ALTER TABLE goal_runs ADD COLUMN next_retry_at_ms INTEGER;

ALTER TABLE goal_runs ADD COLUMN pause_reason TEXT;

ALTER TABLE goal_runs ADD COLUMN last_error TEXT;

UPDATE goal_runs SET failure_retries = 2, next_retry_at_ms = 7000,
    pause_reason = 'retry_backoff', last_error = 'Synthetic transport timeout.'
WHERE goal_id = 'synthetic-goal-paused';
