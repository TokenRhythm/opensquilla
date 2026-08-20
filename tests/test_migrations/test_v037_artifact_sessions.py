"""Upgrade and rollback coverage for the V037 ArtifactSession schema."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from opensquilla.artifact_session.schema import SCHEMA_OBJECTS, SCHEMA_STATEMENTS
from opensquilla.persistence.migrator import apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_ID = "V037__artifact_sessions"
ARTIFACT_MIGRATION_IDS = (
    MIGRATION_ID,
    "V038__artifact_prompt_annotations",
    "V039__artifact_mutation_attempts",
    "V040__document_resources",
)

TABLES = {
    "artifact_documents",
    "artifact_revisions",
    "artifact_change_sets",
    "artifact_anchors",
    "artifact_edit_sessions",
    "artifact_writer_leases",
    "artifact_audit_events",
}


def _artifact_schema(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    placeholders = ", ".join("?" for _ in SCHEMA_OBJECTS)
    rows = conn.execute(
        f"SELECT name, type, sql FROM sqlite_master WHERE name IN ({placeholders})",  # noqa: S608
        SCHEMA_OBJECTS,
    ).fetchall()
    return {
        str(name): (str(kind), re.sub(r"\s+", " ", str(sql)).strip())
        for name, kind, sql in rows
    }


def _apply_origin_main_profile(db_path: Path) -> None:
    backend = get_backend("sqlite:///" + str(db_path))
    try:
        migrations = read_migrations(str(MIGRATIONS_DIR)).filter(
            lambda item: item.id not in ARTIFACT_MIGRATION_IDS
        )
        with backend.lock():
            backend.apply_migrations(backend.to_apply(migrations))
    finally:
        backend.connection.close()


def test_v037_through_v040_upgrade_origin_main_profile(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    _apply_origin_main_profile(db_path)

    with sqlite3.connect(db_path) as conn:
        applied_before = {
            str(row[0])
            for row in conn.execute("SELECT migration_id FROM _yoyo_migration").fetchall()
        }
    assert "V035__pending_chat_inputs" in applied_before
    assert not set(ARTIFACT_MIGRATION_IDS) & applied_before

    assert apply_pending(str(db_path), MIGRATIONS_DIR) == list(ARTIFACT_MIGRATION_IDS)


def test_v037_creates_complete_artifact_session_schema_and_guards_revisions(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    assert MIGRATION_ID in apply_pending(str(db_path), MIGRATIONS_DIR)

    with sqlite3.connect(db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        triggers = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        indexes = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert TABLES <= tables
        assert {
            "artifact_revisions_immutable",
            "artifact_anchors_immutable",
            "artifact_audit_events_immutable",
        } <= triggers
        assert {
            "idx_artifact_documents_session",
            "idx_artifact_revisions_document",
            "idx_artifact_revisions_artifact",
            "idx_artifact_change_sets_document_status",
            "idx_artifact_change_sets_turn",
            "idx_artifact_edit_sessions_expiry",
            "idx_artifact_writer_leases_expiry",
            "idx_artifact_audit_document_sequence",
        } <= indexes

        conn.execute(
            """
            INSERT INTO artifact_documents (
                document_id, session_key, name, kind, head_revision_id,
                generation, state_revision, created_at, updated_at
            ) VALUES ('doc-1', 'session-1', 'Report', 'document', 'rev-1', 1, 1, 1, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO artifact_revisions (
                revision_id, document_id, generation, artifact_id,
                artifact_sha256, filename, media_type, byte_size, source,
                actor_kind, actor_id, created_at
            ) VALUES (
                'rev-1', 'doc-1', 1, 'artifact-1',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'report.docx', 'application/octet-stream', 10, 'initial',
                'user', 'user-1', 1
            )
            """
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="revisions are immutable"):
            conn.execute(
                "UPDATE artifact_revisions SET filename = 'changed.docx' "
                "WHERE revision_id = 'rev-1'"
            )


def test_v037_and_runtime_initializer_create_the_same_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    apply_pending(str(db_path), MIGRATIONS_DIR)

    with sqlite3.connect(":memory:") as runtime, sqlite3.connect(db_path) as migrated:
        for statement in SCHEMA_STATEMENTS:
            runtime.execute(statement)
        runtime_schema = _artifact_schema(runtime)
        migrated_schema = _artifact_schema(migrated)

    assert set(runtime_schema) == set(SCHEMA_OBJECTS)
    assert migrated_schema == runtime_schema


def test_v037_rolls_back_only_artifact_session_objects(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    apply_pending(str(db_path), MIGRATIONS_DIR)

    backend = get_backend("sqlite:///" + str(db_path))
    try:
        migration = read_migrations(str(MIGRATIONS_DIR)).filter(
            lambda item: item.id in ARTIFACT_MIGRATION_IDS
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
        applied = {
            str(row[0])
            for row in conn.execute("SELECT migration_id FROM _yoyo_migration").fetchall()
        }

    assert not TABLES & tables
    assert "V032__meta_launch_discard_tombstones" in applied
    assert MIGRATION_ID not in applied
    assert "V038__artifact_prompt_annotations" not in applied
    assert "V039__artifact_mutation_attempts" not in applied
    assert "V040__document_resources" not in applied
