"""V041 - retain the original enqueue fingerprint in dispatch tombstones."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V040__document_resources"}

TABLE = "pending_chat_input_dispatch_receipts"
COLUMN = "enqueue_request_fingerprint"


def _column_exists(conn) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({TABLE})")
    return any(str(row[1]) == COLUMN for row in cur.fetchall())


def apply_step(conn) -> None:
    if not _column_exists(conn):
        conn.cursor().execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} TEXT")


def rollback_step(conn) -> None:
    if _column_exists(conn):
        conn.cursor().execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")


steps = [step(apply_step, rollback_step)]
