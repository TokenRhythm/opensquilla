"""V021 - durable provider usage ledger.

This migration is intentionally schema-only. Cutover state and legacy session
baselines are initialized by ``SessionStorage.initialize_usage_ledger`` in one
short transaction before live accounting is enabled. Historical transcript
backfill runs separately after gateway readiness.

The ledger tables contain accounting identities and aggregate usage only. They
do not store prompts, transcript content, channel identifiers, or session keys.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V020__turn_ingress_receipts"}


CREATE_USAGE_EVENTS = """
CREATE TABLE IF NOT EXISTS usage_events (
    event_id                    TEXT PRIMARY KEY,
    execution_id                TEXT NOT NULL,
    call_index                  INTEGER NOT NULL CHECK (call_index >= 0),
    turn_id                     TEXT,
    agent_run_id                TEXT,
    parent_turn_id              TEXT,
    session_id                  TEXT NOT NULL,
    session_epoch               INTEGER NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    agent_id                    TEXT NOT NULL DEFAULT 'main',
    run_kind                    TEXT NOT NULL DEFAULT 'default',
    provider                    TEXT,
    model                       TEXT,
    started_at_ms               INTEGER NOT NULL CHECK (started_at_ms >= 0),
    completed_at_ms             INTEGER,
    status                      TEXT NOT NULL DEFAULT 'started'
                                CHECK (status IN ('started', 'finalized', 'unknown')),
    input_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens               INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    reasoning_tokens            INTEGER NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    cache_read_tokens           INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens          INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    total_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    cost_nanos                  INTEGER NOT NULL DEFAULT 0 CHECK (cost_nanos >= 0),
    billed_cost_nanos           INTEGER NOT NULL DEFAULT 0 CHECK (billed_cost_nanos >= 0),
    estimated_cost_nanos        INTEGER NOT NULL DEFAULT 0 CHECK (estimated_cost_nanos >= 0),
    cost_source                 TEXT NOT NULL DEFAULT 'none',
    estimate_basis              TEXT,
    price_source                TEXT,
    coverage_status             TEXT NOT NULL DEFAULT 'pending',
    missing_cost_entries        INTEGER NOT NULL DEFAULT 0
                                CHECK (missing_cost_entries >= 0),
    unknown_reason              TEXT,
    origin                      TEXT NOT NULL,
    schema_version              INTEGER NOT NULL DEFAULT 1,
    UNIQUE (execution_id, call_index),
    CHECK (completed_at_ms IS NULL OR completed_at_ms >= started_at_ms),
    CHECK (cost_nanos = billed_cost_nanos + estimated_cost_nanos)
)
"""

CREATE_USAGE_EVENT_ITEMS = """
CREATE TABLE IF NOT EXISTS usage_event_items (
    event_id                    TEXT NOT NULL,
    ordinal                     INTEGER NOT NULL CHECK (ordinal >= 0),
    provider                    TEXT,
    model                       TEXT,
    input_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens               INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    reasoning_tokens            INTEGER NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    cache_read_tokens           INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens          INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    total_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    cost_nanos                  INTEGER NOT NULL DEFAULT 0 CHECK (cost_nanos >= 0),
    billed_cost_nanos           INTEGER NOT NULL DEFAULT 0 CHECK (billed_cost_nanos >= 0),
    estimated_cost_nanos        INTEGER NOT NULL DEFAULT 0 CHECK (estimated_cost_nanos >= 0),
    cost_source                 TEXT NOT NULL DEFAULT 'none',
    estimate_basis              TEXT,
    price_source                TEXT,
    schema_version              INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (event_id, ordinal),
    FOREIGN KEY (event_id) REFERENCES usage_events(event_id) ON DELETE CASCADE,
    CHECK (cost_nanos = billed_cost_nanos + estimated_cost_nanos)
)
"""

CREATE_USAGE_LEDGER_STATE = """
CREATE TABLE IF NOT EXISTS usage_ledger_state (
    singleton_id                INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    ledger_started_at_ms        INTEGER NOT NULL CHECK (ledger_started_at_ms >= 0),
    backfill_status             TEXT NOT NULL DEFAULT 'pending'
                                CHECK (backfill_status IN
                                       ('pending', 'running', 'complete',
                                        'partial', 'failed')),
    cursor_created_at_ms        INTEGER,
    cursor_session_id           TEXT,
    cursor_message_id           TEXT,
    backfilled_event_count      INTEGER NOT NULL DEFAULT 0
                                CHECK (backfilled_event_count >= 0),
    backfilled_cost_nanos       INTEGER NOT NULL DEFAULT 0
                                CHECK (backfilled_cost_nanos >= 0),
    anomaly_count               INTEGER NOT NULL DEFAULT 0 CHECK (anomaly_count >= 0),
    last_error_code             TEXT,
    updated_at_ms               INTEGER NOT NULL CHECK (updated_at_ms >= 0),
    schema_version              INTEGER NOT NULL DEFAULT 1,
    CHECK (
        (cursor_created_at_ms IS NULL AND cursor_session_id IS NULL
         AND cursor_message_id IS NULL)
        OR
        (cursor_created_at_ms IS NOT NULL AND cursor_session_id IS NOT NULL
         AND cursor_message_id IS NOT NULL)
    )
)
"""

CREATE_USAGE_LEGACY_BASELINES = """
CREATE TABLE IF NOT EXISTS usage_legacy_baselines (
    session_id                  TEXT NOT NULL,
    session_epoch               INTEGER NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    agent_id                    TEXT NOT NULL DEFAULT 'main',
    captured_at_ms              INTEGER NOT NULL CHECK (captured_at_ms >= 0),
    input_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens               INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    total_tokens                INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    cache_read_tokens           INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens          INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    cost_nanos                  INTEGER NOT NULL DEFAULT 0 CHECK (cost_nanos >= 0),
    billed_cost_nanos           INTEGER NOT NULL DEFAULT 0 CHECK (billed_cost_nanos >= 0),
    estimated_cost_nanos        INTEGER NOT NULL DEFAULT 0 CHECK (estimated_cost_nanos >= 0),
    cost_source                 TEXT NOT NULL DEFAULT 'none',
    missing_cost_entries        INTEGER NOT NULL DEFAULT 0
                                CHECK (missing_cost_entries >= 0),
    schema_version              INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session_id, session_epoch),
    CHECK (cost_nanos = billed_cost_nanos + estimated_cost_nanos)
)
"""

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_usage_events_completed "
    "ON usage_events(completed_at_ms, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_events_session_completed "
    "ON usage_events(session_id, completed_at_ms, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_events_agent_completed "
    "ON usage_events(agent_id, completed_at_ms, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_events_status_completed "
    "ON usage_events(status, completed_at_ms, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_events_status_started "
    "ON usage_events(status, started_at_ms, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_event_items_model "
    "ON usage_event_items(model, event_id, ordinal)",
    "CREATE INDEX IF NOT EXISTS idx_usage_event_items_provider "
    "ON usage_event_items(provider, event_id, ordinal)",
    "CREATE INDEX IF NOT EXISTS idx_usage_legacy_baselines_captured "
    "ON usage_legacy_baselines(captured_at_ms, session_id)",
)

INDEX_NAMES = (
    "idx_usage_events_completed",
    "idx_usage_events_session_completed",
    "idx_usage_events_agent_completed",
    "idx_usage_events_status_completed",
    "idx_usage_events_status_started",
    "idx_usage_event_items_model",
    "idx_usage_event_items_provider",
    "idx_usage_legacy_baselines_captured",
)


def apply_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(CREATE_USAGE_EVENTS)
    cur.execute(CREATE_USAGE_EVENT_ITEMS)
    cur.execute(CREATE_USAGE_LEDGER_STATE)
    cur.execute(CREATE_USAGE_LEGACY_BASELINES)
    for statement in INDEX_STATEMENTS:
        cur.execute(statement)


def rollback_step(conn) -> None:
    cur = conn.cursor()
    for name in reversed(INDEX_NAMES):
        cur.execute(f"DROP INDEX IF EXISTS {name}")
    cur.execute("DROP TABLE IF EXISTS usage_event_items")
    cur.execute("DROP TABLE IF EXISTS usage_events")
    cur.execute("DROP TABLE IF EXISTS usage_legacy_baselines")
    cur.execute("DROP TABLE IF EXISTS usage_ledger_state")


steps = [step(apply_step, rollback_step)]
