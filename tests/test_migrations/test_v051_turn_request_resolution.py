"""New-protocol fences do not reinterpret or backfill historical receipts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import apply_pending

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_ID = "V051__turn_request_resolution"


def test_upgrade_accepts_recovery_receipts_without_fingerprint_and_preserves_fences(tmp_path):
    path = tmp_path / "recovery.sqlite"
    backend = get_backend("sqlite:///" + str(path))
    try:
        previous = read_migrations(str(MIGRATIONS)).filter(lambda item: item.id < MIGRATION_ID)
        with backend.lock():
            backend.apply_migrations(backend.to_apply(previous))
    finally:
        backend.connection.close()
    with sqlite3.connect(path) as conn:
        # A supported recovery fixture has only historical receipt coordinates.
        conn.execute("DROP TABLE turn_ingress_receipts")
        conn.execute("CREATE TABLE turn_ingress_receipts (receipt_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO turn_ingress_receipts VALUES ('legacy-evidence')")
    assert apply_pending(str(path), MIGRATIONS) == [
        MIGRATION_ID, "V052__turn_request_resolution_validity",
    ]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM turn_ingress_receipts").fetchall() == [
            ("legacy-evidence",),
        ]
        assert conn.execute("SELECT count(*) FROM turn_request_resolution").fetchone() == (0,)
        conn.execute(
            "INSERT INTO turn_request_resolution "
            "(source_scope, request_session_key, client_request_id, request_fingerprint, "
            "state, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("web:webchat:operator:resolvable.v1", "key", "request", "hash", "retired", 1),
        )
    backend = get_backend("sqlite:///" + str(path))
    try:
        current = read_migrations(str(MIGRATIONS)).filter(lambda item: item.id == MIGRATION_ID)
        with backend.lock():
            backend.rollback_migrations(current)
    finally:
        backend.connection.close()
    # Downgrade/re-upgrade cannot erase terminal evidence of a delayed send.
    assert apply_pending(str(path), MIGRATIONS) == [MIGRATION_ID]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT state FROM turn_request_resolution").fetchall() == [
            ("retired",),
        ]
