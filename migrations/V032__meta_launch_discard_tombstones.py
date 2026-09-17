"""V032 - terminal, content-free cancellation markers for MetaSkill drafts.

An explicit discard must win over a delayed or replayed ``meta.run`` request
using the same browser ingress identity. The marker contains only request
coordinates and expires under the same bounded runtime retention policy as the
raw launch draft.
"""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V031__meta_launch_drafts"}

TABLE = "meta_launch_discard_tombstones"
EXPIRY_INDEX = "idx_meta_launch_discard_tombstones_expiry"

CREATE_TABLE = f"""
CREATE TABLE {TABLE} (
    session_key         TEXT NOT NULL,
    client_request_id   TEXT NOT NULL,
    created_at          INTEGER NOT NULL,
    expires_at          INTEGER NOT NULL,
    schema_version      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session_key, client_request_id)
)
"""

CREATE_EXPIRY_INDEX = (
    f"CREATE INDEX IF NOT EXISTS {EXPIRY_INDEX} "
    f"ON {TABLE}(expires_at, created_at)"
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
    # yoyo records this migration, so always reconcile its supporting index.
    cur.execute(CREATE_EXPIRY_INDEX)


def rollback_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(f"DROP INDEX IF EXISTS {EXPIRY_INDEX}")
    cur.execute(f"DROP TABLE IF EXISTS {TABLE}")


steps = [step(apply_step, rollback_step)]
