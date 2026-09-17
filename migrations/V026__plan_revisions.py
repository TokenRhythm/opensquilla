"""V026 - immutable structured plan revisions."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V025__session_collaboration_state"}

CREATE_PLAN_REVISIONS = """
CREATE TABLE IF NOT EXISTS plan_revisions (
    revision_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    parent_revision_id TEXT,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    source_session_key TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    source_epoch INTEGER NOT NULL DEFAULT 0 CHECK (source_epoch >= 0),
    source_turn_id TEXT,
    source_message_id TEXT,
    title TEXT NOT NULL,
    markdown TEXT NOT NULL,
    steps TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
)
"""

CREATE_INDEXES: tuple[str, ...] = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_revisions_plan_generation
    ON plan_revisions(plan_id, generation)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_plan_revisions_source_session
    ON plan_revisions(source_session_key, created_at)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_revisions_source_message
    ON plan_revisions(source_session_id, source_message_id)
    WHERE source_message_id IS NOT NULL
    """,
)

CREATE_IMMUTABLE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS plan_revisions_immutable
BEFORE UPDATE ON plan_revisions
BEGIN
    SELECT RAISE(ABORT, 'plan revisions are immutable');
END
"""


def apply_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(CREATE_PLAN_REVISIONS)
    for statement in CREATE_INDEXES:
        cur.execute(statement)
    cur.execute(CREATE_IMMUTABLE_TRIGGER)


def rollback_step(conn) -> None:
    cur = conn.cursor()
    cur.execute("DROP TRIGGER IF EXISTS plan_revisions_immutable")
    cur.execute("DROP TABLE IF EXISTS plan_revisions")


steps = [step(apply_step, rollback_step)]
