"""V030 - durable hidden MetaSkill control intents.

``meta.run`` and committed failed-step replay previously handed the following
``chat.send`` an in-process marker.  A gateway restart or a long client-side
queue could erase that authorization before the accepted turn reached the
engine.  This content-free ledger preserves the authorization and binds it to
the ingress receipt/task in the same SQLite transaction.

The table stores skill/run identifiers and one-way turn fingerprints only. It
never stores the user request, provider text, credentials, or replay capability
tokens. Session deletion explicitly purges its rows. Rollback is additive and
drops only this table and its indexes.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V029__sandbox_policy_tokens"}

TABLE = "meta_control_intents"
CORRELATION_INDEX = "uq_meta_control_intents_correlation"
SESSION_STATUS_INDEX = "idx_meta_control_intents_session_status"

CREATE_TABLE = f"""
CREATE TABLE {TABLE} (
    intent_id                           TEXT PRIMARY KEY,
    session_key                        TEXT NOT NULL,
    control_kind                       TEXT NOT NULL,
    correlation_id                     TEXT NOT NULL,
    meta_skill_name                    TEXT NOT NULL,
    replay_run_id                      TEXT,
    replay_mode                        TEXT,
    status                             TEXT NOT NULL DEFAULT 'staged',
    accepted_source_scope              TEXT,
    accepted_request_session_key       TEXT,
    accepted_client_request_id         TEXT,
    accepted_request_fingerprint       TEXT,
    accepted_message_id                TEXT,
    accepted_task_id                   TEXT,
    created_at                         INTEGER NOT NULL,
    updated_at                         INTEGER NOT NULL,
    schema_version                     INTEGER NOT NULL DEFAULT 1,
    CHECK (control_kind IN ('manual', 'replay')),
    CHECK (status IN ('staged', 'accepted'))
)
"""

CREATE_CORRELATION_INDEX = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {CORRELATION_INDEX} "
    f"ON {TABLE}(session_key, control_kind, correlation_id)"
)

CREATE_SESSION_STATUS_INDEX = (
    f"CREATE INDEX IF NOT EXISTS {SESSION_STATUS_INDEX} "
    f"ON {TABLE}(session_key, status, created_at)"
)


def _table_exists(conn, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def apply_step(conn) -> None:
    cur = conn.cursor()
    if not _table_exists(conn, TABLE):
        cur.execute(CREATE_TABLE)
    # SessionStorage can create the compatible table before yoyo records this
    # migration. Keep index creation outside the guard so that path still gains
    # the uniqueness and recovery-query guarantees.
    cur.execute(CREATE_CORRELATION_INDEX)
    cur.execute(CREATE_SESSION_STATUS_INDEX)


def rollback_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(f"DROP INDEX IF EXISTS {SESSION_STATUS_INDEX}")
    cur.execute(f"DROP INDEX IF EXISTS {CORRELATION_INDEX}")
    cur.execute(f"DROP TABLE IF EXISTS {TABLE}")


steps = [step(apply_step, rollback_step)]
