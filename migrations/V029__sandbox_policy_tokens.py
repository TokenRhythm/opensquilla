"""V029 - named sandbox tokens and versioned sandbox policy storage."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V028__project_workspaces"}


CREATE_TOKENS = """
CREATE TABLE IF NOT EXISTS sandbox_tokens (
    public_id TEXT PRIMARY KEY,
    token_version INTEGER NOT NULL,
    name TEXT NOT NULL,
    secret_digest BLOB NOT NULL,
    roles_json TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    last_peer TEXT,
    revoked_at INTEGER
)
"""

CREATE_TOKEN_INDEX = """
CREATE INDEX IF NOT EXISTS idx_sandbox_tokens_active
ON sandbox_tokens(revoked_at, created_at)
"""

CREATE_POLICY = """
CREATE TABLE IF NOT EXISTS sandbox_policy (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    policy_version INTEGER NOT NULL,
    policy_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
)
"""

CREATE_PREFERENCES = """
CREATE TABLE IF NOT EXISTS sandbox_execution_preferences (
    client_id TEXT PRIMARY KEY,
    desired_mode TEXT NOT NULL CHECK (desired_mode IN ('safe', 'full')),
    updated_at INTEGER NOT NULL
)
"""

steps = [
    step(CREATE_TOKENS, "DROP TABLE IF EXISTS sandbox_tokens"),
    step(CREATE_TOKEN_INDEX, "DROP INDEX IF EXISTS idx_sandbox_tokens_active"),
    step(CREATE_POLICY, "DROP TABLE IF EXISTS sandbox_policy"),
    step(CREATE_PREFERENCES, "DROP TABLE IF EXISTS sandbox_execution_preferences"),
]
