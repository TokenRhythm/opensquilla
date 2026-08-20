"""Upgrade and rollback coverage for artifact mutation attempt receipts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import apply_pending

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_ID = "V039__artifact_mutation_attempts"


def _apply_through_v038(db_path: Path) -> None:
    backend = get_backend("sqlite:///" + str(db_path))
    try:
        migrations = read_migrations(str(MIGRATIONS_DIR)).filter(
            lambda item: item.id != MIGRATION_ID
        )
        with backend.lock():
            backend.apply_migrations(backend.to_apply(migrations))
    finally:
        backend.connection.close()


def _seed_document(conn: sqlite3.Connection, suffix: str) -> None:
    document_id = f"doc-{suffix}"
    revision_id = f"rev-{suffix}"
    conn.execute(
        """
        INSERT INTO artifact_documents (
            document_id, session_key, session_id, name, kind, head_revision_id,
            generation, state_revision, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'html', ?, 1, 1, 1, 1)
        """,
        (
            document_id,
            f"key-{suffix}",
            f"session-{suffix}",
            f"Page {suffix}",
            revision_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO artifact_revisions (
            revision_id, document_id, generation, artifact_id, artifact_sha256,
            filename, media_type, byte_size, source, actor_kind, actor_id, created_at
        ) VALUES (
            ?, ?, 1, ?,
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'page.html', 'text/html', 10, 'initial', 'user', 'user-1', 1
        )
        """,
        (revision_id, document_id, f"artifact-{suffix}"),
    )


def test_v039_upgrades_v038_profile_and_enforces_receipt_constraints(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    _apply_through_v038(db_path)
    with sqlite3.connect(db_path) as conn:
        _seed_document(conn, "1")
        _seed_document(conn, "2")
        conn.commit()

    assert apply_pending(str(db_path), MIGRATIONS_DIR) == [MIGRATION_ID]

    with sqlite3.connect(db_path) as conn:
        column_rows = tuple(conn.execute("PRAGMA table_info(artifact_mutation_attempts)"))
        columns = {
            str(row[1])
            for row in column_rows
        }
        assert columns == {
            "mutation_attempt_id",
            "document_id",
            "turn_id",
            "tool_use_id",
            "base_revision_id",
            "proposal_sha256",
            "status",
            "change_set_id",
            "revision_id",
            "failure_code",
            "candidate_session_id",
            "candidate_artifact_id",
            "candidate_artifact_sha256",
            "candidate_registered_at",
            "state_revision",
            "created_at",
            "updated_at",
            "schema_version",
        }
        proposal_column = next(row for row in column_rows if row[1] == "proposal_sha256")
        assert proposal_column[3] == 0
        conn.execute(
            """
            INSERT INTO artifact_mutation_attempts (
                mutation_attempt_id, document_id, turn_id, tool_use_id,
                base_revision_id, proposal_sha256, status,
                state_revision, created_at, updated_at
            ) VALUES ('attempt-1', 'doc-1', 'turn-1', 'tool-1', 'rev-1',
                      ?, 'reserved', 1, 1, 1)
            """,
            ("d" * 64,),
        )
        conn.execute(
            """
            INSERT INTO artifact_mutation_attempts (
                mutation_attempt_id, document_id, turn_id, tool_use_id,
                base_revision_id, status, state_revision, created_at, updated_at
            ) VALUES ('attempt-null', 'doc-1', 'turn-null', 'tool-null', 'rev-1',
                      'reserved', 1, 1, 1)
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO artifact_mutation_attempts (
                    mutation_attempt_id, document_id, turn_id, tool_use_id,
                    base_revision_id, proposal_sha256, status,
                    state_revision, created_at, updated_at
                ) VALUES ('attempt-2', 'doc-2', 'turn-1', 'tool-2', 'rev-2',
                          ?, 'reserved', 1, 1, 1)
                """,
                ("e" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE artifact_mutation_attempts SET proposal_sha256='INVALID' "
                "WHERE mutation_attempt_id='attempt-1'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE artifact_mutation_attempts SET proposal_sha256=? "
                "WHERE mutation_attempt_id='attempt-1'",
                ("z" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE artifact_mutation_attempts SET status='failed', failure_code=? "
                "WHERE mutation_attempt_id='attempt-1'",
                ("x" * 129,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE artifact_mutation_attempts SET candidate_artifact_id='art-partial' "
                "WHERE mutation_attempt_id='attempt-1'"
            )
        conn.execute(
            """
            UPDATE artifact_mutation_attempts
            SET candidate_session_id='session-1', candidate_artifact_id='art-fixed',
                candidate_artifact_sha256=?, candidate_registered_at=2
            WHERE mutation_attempt_id='attempt-1'
            """,
            ("b" * 64,),
        )


def test_v039_rolls_back_only_mutation_attempt_receipts(tmp_path: Path) -> None:
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
            )
        }
    assert "artifact_mutation_attempts" not in tables
    assert "artifact_prompt_annotations" in tables
