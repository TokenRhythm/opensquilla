"""Upgrade and rollback coverage for original pending enqueue fingerprints."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_ID = "V041__pending_input_enqueue_fingerprint"


def _columns(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(pending_chat_input_dispatch_receipts)")
        }


def _apply_through_v040(db_path: Path) -> None:
    backend = get_backend("sqlite:///" + str(db_path))
    try:
        migrations = read_migrations(str(MIGRATIONS_DIR)).filter(
            lambda item: item.id != MIGRATION_ID
        )
        with backend.lock():
            backend.apply_migrations(backend.to_apply(migrations))
    finally:
        backend.connection.close()


def test_v041_adds_nullable_original_enqueue_fingerprint_without_losing_receipts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    _apply_through_v040(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO pending_chat_input_dispatch_receipts (
                pending_input_id, session_key, source_scope, client_request_id,
                client_message_id, request_fingerprint, accepted_at
            ) VALUES ('pending-1', 'session-1', 'rpc:web', 'request-1',
                      'message-1', 'sha256:material', 1)
            """
        )
        conn.commit()

    assert apply_pending(str(db_path), MIGRATIONS_DIR) == [MIGRATION_ID]

    assert "enqueue_request_fingerprint" in _columns(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT request_fingerprint, enqueue_request_fingerprint
            FROM pending_chat_input_dispatch_receipts
            WHERE pending_input_id = 'pending-1'
            """
        ).fetchone()
    assert row == ("sha256:material", None)


def test_v041_rolls_back_only_original_enqueue_fingerprint(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    apply_pending(str(db_path), MIGRATIONS_DIR)
    backend = get_backend("sqlite:///" + str(db_path))
    try:
        migration = read_migrations(str(MIGRATIONS_DIR)).filter(
            lambda item: item.id == MIGRATION_ID
        )
        with backend.lock():
            backend.rollback_migrations(migration)
    finally:
        backend.connection.close()

    columns = _columns(db_path)
    assert "enqueue_request_fingerprint" not in columns
    assert "request_fingerprint" in columns
