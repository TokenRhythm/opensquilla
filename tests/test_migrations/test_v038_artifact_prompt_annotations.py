"""Upgrade and rollback coverage for durable prompt-annotation drafts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_ID = "V038__artifact_prompt_annotations"


def test_v038_creates_prompt_annotation_constraints_and_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    assert MIGRATION_ID in apply_pending(str(db_path), MIGRATIONS_DIR)

    with sqlite3.connect(db_path) as conn:
        columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(artifact_prompt_annotations)"
            ).fetchall()
        }
        assert columns == {
            "annotation_id",
            "session_key",
            "session_id",
            "session_epoch",
            "document_id",
            "revision_id",
            "anchor_id",
            "body",
            "status",
            "state_revision",
            "sent_message_id",
            "sent_turn_id",
            "sent_order",
            "created_at",
            "updated_at",
            "schema_version",
        }
        indexes = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert {
            "idx_artifact_prompt_annotations_session_status",
            "idx_artifact_prompt_annotations_document_revision",
        } <= indexes

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
        conn.execute(
            """
            INSERT INTO artifact_anchors (
                anchor_id, document_id, revision_id, kind, locator_json,
                state, created_at
            ) VALUES ('anchor-1', 'doc-1', 'rev-1', 'dom_source', '{}', 'resolved', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO artifact_prompt_annotations (
                annotation_id, session_key, session_id, session_epoch,
                document_id, revision_id, anchor_id, body, status,
                state_revision, created_at, updated_at
            ) VALUES (
                'annotation-1', 'key-1', 'session-1', 0,
                'doc-1', 'rev-1', 'anchor-1', '', 'draft', 1, 1, 1
            )
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE artifact_prompt_annotations SET body = ? WHERE annotation_id = ?",
                ("😀" * 4097, "annotation-1"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE artifact_prompt_annotations SET status = 'sent' "
                "WHERE annotation_id = 'annotation-1'"
            )


def test_v038_rolls_back_only_prompt_annotations(tmp_path: Path) -> None:
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
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "artifact_prompt_annotations" not in tables
    assert "artifact_documents" in tables
