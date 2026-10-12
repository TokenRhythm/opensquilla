"""V020 - durable idempotency receipts for accepted turns.

Creates ``turn_ingress_receipts``: one row for each turn request after its
database acceptance transaction commits.  The request identity is scoped by
``(source_scope, request_session_key, client_request_id)`` so a client can
repeat an ambiguous send without creating another transcript entry or task.
``request_fingerprint`` lets the ingress layer reject reuse of the same key
for a different payload.

The accepted identifiers deliberately have no foreign keys.  Session tables
are initialized lazily by ``SessionStorage.connect()`` and accepted tasks may
be activated only after this row commits.  ``task_id`` is therefore nullable
for accepted request kinds that do not create a new runtime task.

Privacy contract: this table stores identifiers and a one-way request
fingerprint, never message or attachment content.  The migration also adds a
retention-order index to the existing ``turn_errors`` table so bounded cleanup
does not scan and sort the full table while holding the shared write lock.
Rollback drops both additive indexes and the idempotency ledger.  An older
build must first roll back V020 before opening a database that records it, as
enforced by ``assert_schema_not_ahead`` in ``persistence/migrator.py``.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V019__turn_errors"}


TABLE = "turn_ingress_receipts"
REQUEST_UNIQUE_INDEX = "uq_turn_ingress_receipts_request"
ACCEPTED_SESSION_INDEX = "idx_turn_ingress_receipts_accepted_session"
TURN_ERRORS_RETENTION_INDEX = "idx_turn_errors_ts_error"

CREATE_TABLE = f"""
CREATE TABLE {TABLE} (
    receipt_id             TEXT PRIMARY KEY,
    source_scope           TEXT NOT NULL,
    request_session_key    TEXT NOT NULL,
    client_request_id      TEXT NOT NULL,
    request_fingerprint    TEXT NOT NULL,
    accepted_session_key   TEXT NOT NULL,
    session_id             TEXT NOT NULL,
    message_id             TEXT NOT NULL,
    task_id                TEXT,
    accepted_at            INTEGER NOT NULL,
    schema_version         INTEGER NOT NULL DEFAULT 1
)
"""

CREATE_REQUEST_UNIQUE_INDEX = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {REQUEST_UNIQUE_INDEX} "
    f"ON {TABLE}(source_scope, request_session_key, client_request_id)"
)

CREATE_ACCEPTED_SESSION_INDEX = (
    f"CREATE INDEX IF NOT EXISTS {ACCEPTED_SESSION_INDEX} "
    f"ON {TABLE}(accepted_session_key, accepted_at)"
)

CREATE_TURN_ERRORS_RETENTION_INDEX = (
    f"CREATE INDEX IF NOT EXISTS {TURN_ERRORS_RETENTION_INDEX} "
    "ON turn_errors(ts_ms, error_id)"
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
    # Keep this outside the table guard so an out-of-band compatible table
    # still receives the idempotency guarantee when V020 is applied.
    cur.execute(CREATE_REQUEST_UNIQUE_INDEX)
    cur.execute(CREATE_ACCEPTED_SESSION_INDEX)
    cur.execute(CREATE_TURN_ERRORS_RETENTION_INDEX)


def rollback_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(f"DROP INDEX IF EXISTS {TURN_ERRORS_RETENTION_INDEX}")
    cur.execute(f"DROP INDEX IF EXISTS {ACCEPTED_SESSION_INDEX}")
    cur.execute(f"DROP INDEX IF EXISTS {REQUEST_UNIQUE_INDEX}")
    cur.execute(f"DROP TABLE IF EXISTS {TABLE}")


steps = [step(apply_step, rollback_step)]
