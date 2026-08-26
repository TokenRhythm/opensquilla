"""V025 - durable state and decision ledger for fixed-four-tier v2 routing.

The state table is the semantic-task authority used by the pure routing
machine.  A request-claim lease makes classification idempotent and provides
crash reconciliation.  The decision table records classification before
deployment preflight, then receives preflight/execution settlement updates.
None of these tables stores prompt or response text; inputs and outputs remain
linked through existing transcript/usage-ledger identifiers and content hashes.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V024__usage_native_billing_receipts"}


CREATE_STATES = """
CREATE TABLE IF NOT EXISTS fixed_four_tier_states (
    session_id          TEXT PRIMARY KEY,
    session_key         TEXT NOT NULL,
    session_epoch       INTEGER NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    version             INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    task_id             TEXT NOT NULL,
    tier                TEXT NOT NULL CHECK (tier IN ('c0', 'c1', 'c2', 'c3')),
    task_turn_count     INTEGER NOT NULL DEFAULT 0 CHECK (task_turn_count >= 0),
    task_start_input_message_id TEXT,
    last_request_id     TEXT,
    last_route_id       TEXT,
    updated_at_ms       INTEGER NOT NULL CHECK (updated_at_ms >= 0),
    schema_version      INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1),
    UNIQUE (session_key, session_epoch)
)
"""

CREATE_REQUEST_CLAIMS = """
CREATE TABLE IF NOT EXISTS fixed_four_tier_request_claims (
    claim_id             TEXT PRIMARY KEY,
    session_id           TEXT NOT NULL,
    session_key          TEXT NOT NULL,
    session_epoch        INTEGER NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    request_id           TEXT NOT NULL,
    execution_id         TEXT NOT NULL,
    input_message_id     TEXT NOT NULL,
    claimed_at_ms        INTEGER NOT NULL CHECK (claimed_at_ms >= 0),
    updated_at_ms        INTEGER NOT NULL CHECK (updated_at_ms >= 0),
    lease_expires_at_ms  INTEGER NOT NULL CHECK (lease_expires_at_ms >= claimed_at_ms),
    status               TEXT NOT NULL DEFAULT 'claimed'
                         CHECK (status IN
                                ('claimed', 'materialized', 'succeeded', 'failed', 'cancelled')),
    route_id             TEXT,
    terminal_at_ms       INTEGER CHECK (terminal_at_ms IS NULL OR terminal_at_ms >= 0),
    error_code           TEXT,
    schema_version       INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1),
    UNIQUE (session_id, request_id)
)
"""

CREATE_DECISIONS = """
CREATE TABLE IF NOT EXISTS fixed_four_tier_decisions (
    route_id             TEXT PRIMARY KEY,
    session_id           TEXT NOT NULL,
    session_key          TEXT NOT NULL,
    session_epoch        INTEGER NOT NULL DEFAULT 0 CHECK (session_epoch >= 0),
    claim_id             TEXT NOT NULL UNIQUE,
    request_id           TEXT NOT NULL,
    execution_id         TEXT NOT NULL,
    input_message_id     TEXT NOT NULL,
    task_id              TEXT NOT NULL,
    redo_parent_route_id TEXT,
    decided_at_ms        INTEGER NOT NULL CHECK (decided_at_ms >= 0),
    updated_at_ms        INTEGER NOT NULL CHECK (updated_at_ms >= 0),
    intent               TEXT NOT NULL,
    tier                 TEXT NOT NULL,
    previous_tier        TEXT CHECK (
                            previous_tier IS NULL
                            OR previous_tier IN ('c0', 'c1', 'c2', 'c3')
                         ),
    final_tier           TEXT NOT NULL CHECK (final_tier IN ('c0', 'c1', 'c2', 'c3')),
    task_turn_index      INTEGER NOT NULL DEFAULT 0 CHECK (task_turn_index >= 0),
    task_start_input_message_id TEXT,
    context_action       TEXT NOT NULL CHECK (context_action IN ('keep', 'reset')),
    state_version_before INTEGER CHECK (
                            state_version_before IS NULL OR state_version_before >= 0
                         ),
    state_version_after  INTEGER CHECK (
                            state_version_after IS NULL OR state_version_after >= 0
                         ),
    selected_provider    TEXT,
    selected_model       TEXT,
    reasoning            TEXT,
    deployment_version   TEXT,
    config_version       TEXT,
    executed_provider    TEXT,
    executed_model       TEXT,
    executed_deployment_version TEXT,
    usage_summary        TEXT,
    preflight_status     TEXT NOT NULL DEFAULT 'pending'
                         CHECK (preflight_status IN ('pending', 'passed', 'failed')),
    state_committed      INTEGER NOT NULL DEFAULT 0 CHECK (state_committed IN (0, 1)),
    execution_status     TEXT NOT NULL DEFAULT 'pending'
                         CHECK (execution_status IN
                                ('pending', 'succeeded', 'failed', 'cancelled')),
    response_id          TEXT,
    error_code           TEXT,
    terminal_at_ms       INTEGER CHECK (terminal_at_ms IS NULL OR terminal_at_ms >= 0),
    route_trace          TEXT NOT NULL,
    schema_version       INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1),
    UNIQUE (session_id, request_id)
)
"""

INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_fixed_four_tier_states_key "
    "ON fixed_four_tier_states(session_key, session_epoch)",
    "CREATE INDEX IF NOT EXISTS idx_fixed_four_tier_claims_session_status "
    "ON fixed_four_tier_request_claims(session_id, status, updated_at_ms)",
    "CREATE INDEX IF NOT EXISTS idx_fixed_four_tier_claims_lease "
    "ON fixed_four_tier_request_claims(status, lease_expires_at_ms)",
    "CREATE INDEX IF NOT EXISTS idx_fixed_four_tier_claims_execution "
    "ON fixed_four_tier_request_claims(execution_id)",
    "CREATE INDEX IF NOT EXISTS idx_fixed_four_tier_claims_route "
    "ON fixed_four_tier_request_claims(route_id)",
    "CREATE INDEX IF NOT EXISTS idx_fixed_four_tier_decisions_session_time "
    "ON fixed_four_tier_decisions(session_id, decided_at_ms, route_id)",
    "CREATE INDEX IF NOT EXISTS idx_fixed_four_tier_decisions_input "
    "ON fixed_four_tier_decisions(session_id, input_message_id, decided_at_ms)",
    "CREATE INDEX IF NOT EXISTS idx_fixed_four_tier_decisions_request "
    "ON fixed_four_tier_decisions(request_id)",
    "CREATE INDEX IF NOT EXISTS idx_fixed_four_tier_decisions_execution "
    "ON fixed_four_tier_decisions(execution_id)",
    "CREATE INDEX IF NOT EXISTS idx_fixed_four_tier_decisions_key "
    "ON fixed_four_tier_decisions(session_key, session_epoch, decided_at_ms)",
    "CREATE INDEX IF NOT EXISTS idx_fixed_four_tier_decisions_task_time "
    "ON fixed_four_tier_decisions(session_id, task_id, decided_at_ms)",
    "CREATE INDEX IF NOT EXISTS idx_fixed_four_tier_decisions_response "
    "ON fixed_four_tier_decisions(response_id)",
    "CREATE INDEX IF NOT EXISTS idx_fixed_four_tier_decisions_status_time "
    "ON fixed_four_tier_decisions(execution_status, updated_at_ms)",
)

INDEX_NAMES = (
    "idx_fixed_four_tier_states_key",
    "idx_fixed_four_tier_claims_session_status",
    "idx_fixed_four_tier_claims_lease",
    "idx_fixed_four_tier_claims_execution",
    "idx_fixed_four_tier_claims_route",
    "idx_fixed_four_tier_decisions_session_time",
    "idx_fixed_four_tier_decisions_input",
    "idx_fixed_four_tier_decisions_request",
    "idx_fixed_four_tier_decisions_execution",
    "idx_fixed_four_tier_decisions_key",
    "idx_fixed_four_tier_decisions_task_time",
    "idx_fixed_four_tier_decisions_response",
    "idx_fixed_four_tier_decisions_status_time",
)


def apply_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(CREATE_STATES)
    cur.execute(CREATE_REQUEST_CLAIMS)
    cur.execute(CREATE_DECISIONS)
    for statement in INDEX_STATEMENTS:
        cur.execute(statement)


def rollback_step(conn) -> None:
    cur = conn.cursor()
    for name in reversed(INDEX_NAMES):
        cur.execute(f"DROP INDEX IF EXISTS {name}")
    cur.execute("DROP TABLE IF EXISTS fixed_four_tier_decisions")
    cur.execute("DROP TABLE IF EXISTS fixed_four_tier_request_claims")
    cur.execute("DROP TABLE IF EXISTS fixed_four_tier_states")


steps = [step(apply_step, rollback_step)]
