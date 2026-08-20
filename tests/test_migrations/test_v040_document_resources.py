"""Upgrade and rollback coverage for explicit document resource receipts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_ID = "V040__document_resources"


def _apply_through_v039(db_path: Path) -> None:
    backend = get_backend("sqlite:///" + str(db_path))
    try:
        migrations = read_migrations(str(MIGRATIONS_DIR)).filter(
            lambda item: item.id != MIGRATION_ID
        )
        with backend.lock():
            backend.apply_migrations(backend.to_apply(migrations))
    finally:
        backend.connection.close()


def _seed_document(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO artifact_documents (
            document_id, session_key, session_id, name, kind, head_revision_id,
            generation, state_revision, created_at, updated_at
        ) VALUES ('doc-1', 'key-1', 'session-1', 'Page', 'html', 'rev-1', 1, 1, 1, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO artifact_revisions (
            revision_id, document_id, generation, artifact_id, artifact_sha256,
            filename, media_type, byte_size, source, actor_kind, actor_id, created_at
        ) VALUES (
            'rev-1', 'doc-1', 1, 'artifact-1',
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'page.html', 'text/html', 10, 'initial', 'user', 'user-1', 1
        )
        """
    )


def test_v040_upgrades_v039_and_enforces_resource_occurrence_constraints(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    _apply_through_v039(db_path)
    with sqlite3.connect(db_path) as conn:
        _seed_document(conn)
        conn.commit()

    assert apply_pending(str(db_path), MIGRATIONS_DIR) == [MIGRATION_ID]

    with sqlite3.connect(db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "document_source_bindings",
            "document_import_attempts",
            "document_publications",
            "document_publish_attempts",
        } <= tables
        import_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(document_import_attempts)")
        }
        publish_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(document_publish_attempts)")
        }
        assert "candidate_cleaned_at" in import_columns
        assert "promoted_at" in publish_columns
        conn.execute(
            """
            INSERT INTO document_source_bindings (
                binding_id, document_id, session_key, session_id, source_type,
                source_resource_id, source_sha256, source_name, source_mime,
                source_size, mode, created_at
            ) VALUES (
                'binding-1', 'doc-1', 'key-1', 'session-1', 'attachment',
                'att-occurrence-1', ?, 'one.html', 'text/html', 10, 'copy', 1
            )
            """,
            ("b" * 64,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE document_source_bindings SET source_name='changed.html'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO document_source_bindings (
                    binding_id, document_id, session_key, session_id, source_type,
                    source_resource_id, source_sha256, source_name, source_mime,
                    source_size, mode, created_at
                ) VALUES (
                    'binding-2', 'doc-2', 'key-1', 'session-1', 'attachment',
                    'att-occurrence-1', ?, 'two.html', 'text/html', 10, 'copy', 2
                )
                """,
                ("b" * 64,),
            )


def test_v040_rolls_back_only_document_resource_objects(tmp_path: Path) -> None:
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

    with sqlite3.connect(db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "document_source_bindings" not in tables
    assert "document_import_attempts" not in tables
    assert "document_publications" not in tables
    assert "document_publish_attempts" not in tables
    assert "artifact_mutation_attempts" in tables
