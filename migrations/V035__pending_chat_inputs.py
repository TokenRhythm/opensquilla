"""V035 - durable staged chat inputs.

The table is an outbox owned by the Gateway.  A row is intentionally deleted
inside the same transaction that creates the transcript entry, durable task,
and turn-ingress receipt.  Request and message identities are unique per
session, while ``request_fingerprint`` prevents an idempotency key from being
reused for different content.

Attachments are represented only by ``payload_json`` material references.  A
Gateway must materialize an attachment before inserting a row; transient upload
tokens and inline bytes are not valid staged payloads.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V034__goal_message_anchor"}

TABLE = "pending_chat_inputs"
CANCELLATIONS_TABLE = "pending_chat_input_cancellations"
DISPATCH_RECEIPTS_TABLE = "pending_chat_input_dispatch_receipts"

CREATE_TABLE = f"""
CREATE TABLE {TABLE} (
    pending_input_id       TEXT PRIMARY KEY,
    session_key            TEXT NOT NULL,
    source_scope           TEXT NOT NULL,
    client_request_id      TEXT NOT NULL,
    client_message_id      TEXT NOT NULL,
    request_fingerprint    TEXT NOT NULL,
    payload_json           TEXT NOT NULL,
    position               INTEGER NOT NULL DEFAULT 0,
    state_revision         INTEGER NOT NULL DEFAULT 1 CHECK (state_revision >= 1),
    created_at             INTEGER NOT NULL,
    updated_at             INTEGER NOT NULL,
    schema_version         INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
)
"""

CREATE_REQUEST_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_chat_inputs_request
ON {TABLE}(session_key, client_request_id)
"""

CREATE_MESSAGE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_chat_inputs_message
ON {TABLE}(session_key, client_message_id)
"""

CREATE_SESSION_ORDER_INDEX = f"""
CREATE INDEX IF NOT EXISTS idx_pending_chat_inputs_session_order
ON {TABLE}(session_key, position, created_at, pending_input_id)
"""

CREATE_CANCELLATIONS_TABLE = f"""
CREATE TABLE {CANCELLATIONS_TABLE} (
    pending_input_id       TEXT PRIMARY KEY,
    session_key            TEXT NOT NULL,
    cancelled_at           INTEGER NOT NULL,
    schema_version         INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
)
"""

CREATE_CANCELLATIONS_SESSION_INDEX = f"""
CREATE INDEX IF NOT EXISTS idx_pending_chat_input_cancellations_session
ON {CANCELLATIONS_TABLE}(session_key, cancelled_at, pending_input_id)
"""

CREATE_DISPATCH_RECEIPTS_TABLE = f"""
CREATE TABLE {DISPATCH_RECEIPTS_TABLE} (
    pending_input_id       TEXT PRIMARY KEY,
    session_key            TEXT NOT NULL,
    source_scope           TEXT NOT NULL,
    client_request_id      TEXT NOT NULL,
    client_message_id      TEXT NOT NULL,
    request_fingerprint    TEXT NOT NULL,
    accepted_at            INTEGER NOT NULL,
    schema_version         INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
)
"""

CREATE_DISPATCH_REQUEST_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_chat_input_dispatch_request
ON {DISPATCH_RECEIPTS_TABLE}(source_scope, session_key, client_request_id)
"""

CREATE_DISPATCH_MESSAGE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_chat_input_dispatch_message
ON {DISPATCH_RECEIPTS_TABLE}(session_key, client_message_id)
"""

CREATE_DISPATCH_SESSION_INDEX = f"""
CREATE INDEX IF NOT EXISTS idx_pending_chat_input_dispatch_session
ON {DISPATCH_RECEIPTS_TABLE}(session_key, accepted_at, pending_input_id)
"""


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
    if not _table_exists(conn, CANCELLATIONS_TABLE):
        cur.execute(CREATE_CANCELLATIONS_TABLE)
    if not _table_exists(conn, DISPATCH_RECEIPTS_TABLE):
        cur.execute(CREATE_DISPATCH_RECEIPTS_TABLE)
    cur.execute(CREATE_REQUEST_INDEX)
    cur.execute(CREATE_MESSAGE_INDEX)
    cur.execute(CREATE_SESSION_ORDER_INDEX)
    cur.execute(CREATE_CANCELLATIONS_SESSION_INDEX)
    cur.execute(CREATE_DISPATCH_REQUEST_INDEX)
    cur.execute(CREATE_DISPATCH_MESSAGE_INDEX)
    cur.execute(CREATE_DISPATCH_SESSION_INDEX)


def rollback_step(conn) -> None:
    cur = conn.cursor()
    cur.execute("DROP INDEX IF EXISTS idx_pending_chat_input_dispatch_session")
    cur.execute("DROP INDEX IF EXISTS uq_pending_chat_input_dispatch_message")
    cur.execute("DROP INDEX IF EXISTS uq_pending_chat_input_dispatch_request")
    cur.execute("DROP INDEX IF EXISTS idx_pending_chat_input_cancellations_session")
    cur.execute("DROP INDEX IF EXISTS idx_pending_chat_inputs_session_order")
    cur.execute("DROP INDEX IF EXISTS uq_pending_chat_inputs_message")
    cur.execute("DROP INDEX IF EXISTS uq_pending_chat_inputs_request")
    cur.execute(f"DROP TABLE IF EXISTS {DISPATCH_RECEIPTS_TABLE}")
    cur.execute(f"DROP TABLE IF EXISTS {CANCELLATIONS_TABLE}")
    cur.execute(f"DROP TABLE IF EXISTS {TABLE}")


steps = [step(apply_step, rollback_step)]
