"""Office journal upgrades preserve existing documents and recovery receipts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import apply_pending

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_ID = "V048__office_materializations"


def test_existing_profile_upgrade_preserves_documents_and_cascades_journal(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    backend = get_backend("sqlite:///" + str(db_path))
    try:
        previous = read_migrations(str(MIGRATIONS)).filter(lambda item: item.id < MIGRATION_ID)
        with backend.lock():
            backend.apply_migrations(backend.to_apply(previous))
    finally:
        backend.connection.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO artifact_documents "
            "(document_id, session_key, session_id, name, kind, head_revision_id, "
            "generation, state_revision, "
            "created_at, updated_at) VALUES "
            "('doc', 'key', 'session', 'Deck', 'document', 'rev', 1, 1, 1, 1)"
        )
    assert apply_pending(str(db_path), MIGRATIONS) == [MIGRATION_ID]
    assert apply_pending(str(db_path), MIGRATIONS) == []
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute(
            "SELECT name FROM artifact_documents WHERE document_id='doc'"
        ).fetchone() == ("Deck",)
        conn.execute(
            "INSERT INTO artifact_office_materializations "
            "(document_id, operation_id, session_key, session_id, workspace, expected_revision_id, "
            "plan_json, status) VALUES "
            "('doc', 'operation', 'key', 'session', 'workspace', 'rev', '{}', 'prepared')"
        )
        conn.execute("DELETE FROM artifact_documents WHERE document_id='doc'")
        assert conn.execute(
            "SELECT count(*) FROM artifact_office_materializations"
        ).fetchone() == (0,)


def test_downgrade_cannot_discard_unresolved_recovery_receipts(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    apply_pending(str(db_path), MIGRATIONS)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO artifact_documents "
            "(document_id, session_key, session_id, name, kind, head_revision_id, "
            "generation, state_revision, created_at, updated_at) VALUES "
            "('doc', 'key', 'session', 'Deck', 'document', 'rev', 1, 1, 1, 1)"
        )
        conn.execute(
            "INSERT INTO artifact_office_materializations "
            "(document_id, operation_id, session_key, session_id, workspace, expected_revision_id, "
            "plan_json, status) VALUES "
            "('doc', 'operation', 'key', 'session', 'workspace', 'rev', '{}', 'applied')"
        )
    backend = get_backend("sqlite:///" + str(db_path))
    try:
        migration = read_migrations(str(MIGRATIONS)).filter(lambda item: item.id == MIGRATION_ID)
        with backend.lock(), pytest.raises(RuntimeError, match="Resolve pending Office"):
            backend.rollback_migrations(migration)
    finally:
        backend.connection.close()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT status FROM artifact_office_materializations"
        ).fetchone() == ("applied",)
        conn.execute("DELETE FROM artifact_office_materializations")
    backend = get_backend("sqlite:///" + str(db_path))
    try:
        with backend.lock():
            backend.rollback_migrations(migration)
    finally:
        backend.connection.close()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='artifact_office_materializations'"
        ).fetchone() is None
