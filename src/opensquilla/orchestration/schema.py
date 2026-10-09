"""SQLite schema for durable subagent orchestration state."""

from __future__ import annotations

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS orchestration_runs (
        run_id TEXT PRIMARY KEY,
        root_session_id TEXT NOT NULL,
        root_task_id TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('ordinary', 'complex')),
        worker_template_tools_json TEXT NOT NULL,
        lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active', 'completed', 'archived')),
        final_synthesis_completed INTEGER NOT NULL DEFAULT 0
            CHECK (final_synthesis_completed IN (0, 1)),
        created_at INTEGER NOT NULL,
        completed_at INTEGER,
        archived_at INTEGER,
        schema_version INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_sessions (
        session_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        profile TEXT NOT NULL,
        runtime_session_key TEXT NOT NULL,
        lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active', 'idle', 'archived')),
        parent_session_id TEXT,
        depth INTEGER NOT NULL DEFAULT 0 CHECK (depth >= 0),
        effective_tools_json TEXT NOT NULL,
        runtime_context_json TEXT NOT NULL DEFAULT '{}',
        checkpoint_json TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        archived_at INTEGER,
        schema_version INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (run_id) REFERENCES orchestration_runs(run_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS delegated_tasks (
        task_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        parent_task_id TEXT,
        task_key TEXT NOT NULL,
        owner_session_id TEXT NOT NULL,
        description TEXT NOT NULL,
        background INTEGER NOT NULL DEFAULT 0 CHECK (background IN (0, 1)),
        board_status TEXT NOT NULL DEFAULT 'planned' CHECK (
            board_status IN ('planned', 'working', 'completed', 'blocked')
        ),
        acceptance_criteria TEXT,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        board_only INTEGER NOT NULL DEFAULT 0 CHECK (board_only IN (0, 1)),
        outcome TEXT NOT NULL CHECK (
            outcome IN ('pending', 'succeeded', 'failed', 'interrupted', 'timed_out')
        ),
        result_json TEXT,
        retry_of_activation_id TEXT,
        replaces_session_id TEXT,
        effective_tools_json TEXT NOT NULL DEFAULT '[]',
        runtime_context_json TEXT NOT NULL DEFAULT '{}',
        created_at INTEGER NOT NULL,
        finished_at INTEGER,
        schema_version INTEGER NOT NULL DEFAULT 1,
        UNIQUE (run_id, task_key),
        FOREIGN KEY (run_id) REFERENCES orchestration_runs(run_id) ON DELETE CASCADE,
        FOREIGN KEY (owner_session_id) REFERENCES agent_sessions(session_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_session_attachments (
        session_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        parent_session_id TEXT,
        depth INTEGER NOT NULL DEFAULT 0 CHECK (depth >= 0),
        attached_at INTEGER NOT NULL,
        schema_version INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (session_id, run_id),
        FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
        FOREIGN KEY (run_id) REFERENCES orchestration_runs(run_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_activations (
        activation_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        phase TEXT NOT NULL CHECK (
            phase IN ('queued', 'starting', 'running', 'stopping', 'released')
        ),
        live_model_call INTEGER NOT NULL DEFAULT 0 CHECK (live_model_call IN (0, 1)),
        live_tool_call INTEGER NOT NULL DEFAULT 0 CHECK (live_tool_call IN (0, 1)),
        external_wait_id TEXT,
        started_at INTEGER,
        finished_at INTEGER,
        terminal_reason TEXT,
        route_required INTEGER NOT NULL DEFAULT 0 CHECK (route_required IN (0, 1)),
        schema_version INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id),
        FOREIGN KEY (task_id) REFERENCES delegated_tasks(task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_inbox_messages (
        message_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        acknowledged_at INTEGER,
        processed_at INTEGER,
        delivery_owner TEXT,
        delivery_lease_expires_at INTEGER,
        delivery_attempts INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
        idempotency_key TEXT,
        schema_version INTEGER NOT NULL DEFAULT 1,
        UNIQUE (session_id, sequence),
        FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_activations_one_nonterminal
    ON agent_activations(session_id)
    WHERE phase != 'released'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_sessions_parent
    ON agent_sessions(parent_session_id, lifecycle)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_session_attachments_parent
    ON agent_session_attachments(run_id, parent_session_id, depth)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_delegated_tasks_parent_outcome
    ON delegated_tasks(parent_task_id, outcome)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_inbox_pending
    ON agent_inbox_messages(session_id, acknowledged_at, sequence)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_child_result_idempotency
    ON agent_inbox_messages(kind, idempotency_key)
    WHERE kind = 'child_result' AND idempotency_key IS NOT NULL
    """,
)

DROP_STATEMENTS: tuple[str, ...] = (
    "DROP INDEX IF EXISTS idx_agent_child_result_idempotency",
    "DROP INDEX IF EXISTS idx_agent_inbox_pending",
    "DROP INDEX IF EXISTS idx_delegated_tasks_parent_outcome",
    "DROP INDEX IF EXISTS idx_agent_sessions_parent",
    "DROP INDEX IF EXISTS idx_agent_session_attachments_parent",
    "DROP INDEX IF EXISTS idx_agent_activations_one_nonterminal",
    "DROP TABLE IF EXISTS agent_inbox_messages",
    "DROP TABLE IF EXISTS agent_activations",
    "DROP TABLE IF EXISTS delegated_tasks",
    "DROP TABLE IF EXISTS agent_session_attachments",
    "DROP TABLE IF EXISTS agent_sessions",
    "DROP TABLE IF EXISTS orchestration_runs",
)

__all__ = ["DROP_STATEMENTS", "SCHEMA_STATEMENTS"]
