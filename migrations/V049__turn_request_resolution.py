"""V049 - durable retirement fences for registered WebChat attempts."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V048__office_materializations"}


def apply_step(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS turn_request_resolution (
            source_scope TEXT NOT NULL,
            request_session_key TEXT NOT NULL,
            client_request_id TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending', 'retired', 'accepted')),
            updated_at INTEGER NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (source_scope, request_session_key, client_request_id)
        )
    """)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'turn_ingress_receipts'"
    ).fetchone()
    if exists:
        conn.execute("""
            INSERT OR IGNORE INTO turn_request_resolution
            (source_scope, request_session_key, client_request_id, request_fingerprint,
             state, updated_at)
            SELECT source_scope, request_session_key, client_request_id, request_fingerprint,
                   'accepted', accepted_at FROM turn_ingress_receipts
            WHERE source_scope LIKE 'web:webchat:%'
        """)


def rollback_step(conn) -> None:
    # Never erase fences on rollback: an already delayed request may still arrive.
    pass


steps = [step(apply_step, rollback_step)]
