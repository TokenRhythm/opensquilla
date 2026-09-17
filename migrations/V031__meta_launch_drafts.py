"""V031 - bounded durable outbox for unaccepted MetaSkill launch requests.

The browser can close after ``meta.run`` but before the hidden ``chat.send`` is
accepted.  This table retains the exact user-authored launch locally so the Web
UI can resume with the same idempotency identity.  Rows expire after seven days
at runtime, are capacity bounded by ``SessionStorage``, and are purged with
session reset/deletion or atomic turn acceptance.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V030__meta_control_intents"}

TABLE = "meta_launch_drafts"
REQUEST_INDEX = "uq_meta_launch_drafts_request"
SESSION_EXPIRY_INDEX = "idx_meta_launch_drafts_session_expiry"

CREATE_TABLE = f"""
CREATE TABLE {TABLE} (
    draft_id            TEXT PRIMARY KEY,
    session_key         TEXT NOT NULL,
    client_request_id   TEXT NOT NULL,
    meta_skill_name     TEXT NOT NULL,
    launch_text         TEXT NOT NULL,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    expires_at          INTEGER NOT NULL,
    schema_version      INTEGER NOT NULL DEFAULT 1
)
"""

CREATE_REQUEST_INDEX = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {REQUEST_INDEX} "
    f"ON {TABLE}(session_key, client_request_id)"
)

CREATE_SESSION_EXPIRY_INDEX = (
    f"CREATE INDEX IF NOT EXISTS {SESSION_EXPIRY_INDEX} "
    f"ON {TABLE}(session_key, expires_at, created_at)"
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
    # Runtime schema initialization can create the compatible table before
    # yoyo records this migration, so indexes are always reconciled.
    cur.execute(CREATE_REQUEST_INDEX)
    cur.execute(CREATE_SESSION_EXPIRY_INDEX)


def rollback_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(f"DROP INDEX IF EXISTS {SESSION_EXPIRY_INDEX}")
    cur.execute(f"DROP INDEX IF EXISTS {REQUEST_INDEX}")
    cur.execute(f"DROP TABLE IF EXISTS {TABLE}")


steps = [step(apply_step, rollback_step)]
