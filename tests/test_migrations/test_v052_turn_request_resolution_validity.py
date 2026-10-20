"""An existing V051 database keeps its evidence without invented validity."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import apply_pending

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_ID = "V052__turn_request_resolution_validity"


def test_existing_v051_upgrade_and_reapply_preserve_legacy_evidence(tmp_path):
    path = tmp_path / "existing.sqlite"
    backend = get_backend("sqlite:///" + str(path))
    try:
        previous = read_migrations(str(MIGRATIONS)).filter(lambda item: item.id < MIGRATION_ID)
        with backend.lock():
            backend.apply_migrations(backend.to_apply(previous))
    finally:
        backend.connection.close()
    with sqlite3.connect(path) as conn:
        # Reproduce the original six-column schema, not today's shared CREATE.
        conn.execute("DROP TABLE turn_request_resolution")
        conn.execute(
            "CREATE TABLE turn_request_resolution ("
            "source_scope TEXT NOT NULL, request_session_key TEXT NOT NULL, "
            "client_request_id TEXT NOT NULL, request_fingerprint TEXT NOT NULL, "
            "state TEXT NOT NULL, updated_at INTEGER NOT NULL, "
            "PRIMARY KEY(source_scope, request_session_key, client_request_id))"
        )
        conn.execute(
            "INSERT INTO turn_request_resolution VALUES ('scope','key','id','hash','accepted',1)"
        )
    assert apply_pending(str(path), MIGRATIONS) == [MIGRATION_ID]
    assert apply_pending(str(path), MIGRATIONS) == []
    backend = get_backend("sqlite:///" + str(path))
    try:
        current = read_migrations(str(MIGRATIONS)).filter(lambda item: item.id == MIGRATION_ID)
        with backend.lock():
            backend.rollback_migrations(current)
    finally:
        backend.connection.close()
    assert apply_pending(str(path), MIGRATIONS) == [MIGRATION_ID]
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT state, validity_boot_id, validity_expires_hour FROM turn_request_resolution"
        ).fetchall() == [("accepted", "", 0)]
        assert any(
            row[1] == "idx_turn_request_resolution_expiry"
            for row in conn.execute("PRAGMA index_list(turn_request_resolution)")
        )


def test_fresh_database_applies_validity_schema_once(tmp_path):
    path = tmp_path / "fresh.sqlite"
    assert MIGRATION_ID in apply_pending(str(path), MIGRATIONS)
    assert apply_pending(str(path), MIGRATIONS) == []
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM turn_request_resolution").fetchone() == (0,)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(turn_request_resolution)")}
        assert {"validity_boot_id", "validity_expires_hour"} <= columns
