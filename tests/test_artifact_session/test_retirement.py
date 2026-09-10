"""Old editor state is retired without losing ordinary drafts or immutable content."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from opensquilla.artifact_session import ArtifactSessionService
from opensquilla.artifact_session.schema import SCHEMA_STATEMENTS, WORKING_FILES_SCHEMA
from opensquilla.persistence.migrator import apply_pending

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_ID = "V041__retire_html_editor"


def database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    for statement in SCHEMA_STATEMENTS:
        if statement != WORKING_FILES_SCHEMA:
            conn.execute(statement)
    return conn


def seed_change(
    conn: sqlite3.Connection,
    name: str,
    *,
    status: str = "draft",
    audit: str | None = '{"candidate_loop":true}',
    attempt_status: str | None = "reserved",
    revision_matches: bool = True,
    receipt_matches: bool = True,
    audit_document: str | None = None,
) -> None:
    doc, base, change, turn = (f"{prefix}-{name}" for prefix in ("doc", "base", "change", "turn"))
    applied = f"revision-{name}" if status == "applied" else None
    conn.execute(
        """INSERT INTO artifact_documents (
            document_id, session_key, session_id, name, kind, head_revision_id,
            generation, state_revision, created_at, updated_at
        ) VALUES (?, 'session-key', 'session-id', ?, 'html', ?, 1, 1, 1, 1)""",
        (doc, name, base),
    )
    conn.execute(
        """INSERT INTO artifact_revisions (
            revision_id, document_id, generation, artifact_id, artifact_sha256,
            filename, media_type, byte_size, source, actor_kind, actor_id, created_at
        ) VALUES (?, ?, 1, ?, ?, 'page.html', 'text/html', 4, 'initial', 'user', 'user', 1)""",
        (base, doc, f"original-{name}", "a" * 64),
    )
    if applied:
        conn.execute(
            """INSERT INTO artifact_revisions (
                revision_id, document_id, parent_revision_id, generation,
                artifact_id, artifact_sha256, filename, media_type, byte_size,
                source, actor_kind, actor_id, change_set_id, created_at
            ) VALUES (?, ?, ?, 2, ?, ?, 'page.html', 'text/html', 7,
                      'agent', 'agent', 'agent', ?, 2)""",
            (
                applied,
                doc,
                base,
                f"candidate-{name}",
                ("b" if revision_matches else "c") * 64,
                change,
            ),
        )
        conn.execute(
            "UPDATE artifact_documents SET head_revision_id=?, generation=2 WHERE document_id=?",
            (applied, doc),
        )
    conn.execute(
        """INSERT INTO artifact_change_sets (
            change_set_id, document_id, base_revision_id, turn_id, status, operations_json,
            candidate_artifact_id, candidate_artifact_sha256, candidate_filename,
            candidate_media_type, candidate_byte_size, state_revision,
            created_by_kind, created_by_id, applied_revision_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, '[{"op":"replace"}]', ?, ?, 'page.html',
                  'text/html', 7, 1, 'agent', 'agent', ?, 1, 1)""",
        (change, doc, base, turn, status, f"candidate-{name}", "b" * 64, applied),
    )
    if audit is not None:
        conn.execute(
            """INSERT INTO artifact_audit_events (
                event_id, document_id, event_type, actor_kind, actor_id,
                change_set_id, payload_json, created_at
            ) VALUES (?, ?, 'change_set.created', 'agent', 'agent', ?, ?, 1)""",
            (f"audit-{name}", audit_document or doc, change, audit),
        )
    if attempt_status:
        conn.execute(
            """INSERT INTO artifact_mutation_attempts (
                mutation_attempt_id, document_id, turn_id, tool_use_id, base_revision_id,
                status, failure_code, candidate_session_id, candidate_artifact_id,
                candidate_artifact_sha256, candidate_registered_at,
                state_revision, created_at, updated_at
            ) VALUES (?, ?, ?, 'tool', ?, ?, ?, 'session-id', ?, ?, 1, 1, 1, 1)""",
            (
                f"attempt-{name}",
                doc,
                turn,
                base,
                attempt_status,
                "OUTCOME_UNKNOWN" if attempt_status == "ambiguous" else None,
                f"candidate-{name}",
                ("b" if receipt_matches else "d") * 64,
            ),
        )
    conn.commit()


def table_rows(path: Path, table: str) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()  # noqa: S608


async def retire(path: Path, entrypoint: str) -> None:
    if entrypoint == "runtime":
        service = await ArtifactSessionService.open(path)
        try:
            await service.retire_legacy_html_state()
        finally:
            await service.close()
    else:
        migration = next(
            item for item in read_migrations(str(MIGRATIONS)) if item.id == MIGRATION_ID
        )
        migration.load()
        with sqlite3.connect(path) as conn:
            migration.module.apply_step(conn)



@pytest.mark.parametrize("entrypoint", ["runtime", "migration"])
async def test_retirement_requires_exact_audit_attribution_and_retains_bytes(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    path = tmp_path / "state.db"
    untouched = ["absent", "false", "number", "string", "invalid", "other-document"]
    with database(path) as conn:
        for name, audit in zip(
            untouched[:-1],
            [
                None,
                '{"candidate_loop":false}',
                '{"candidate_loop":1}',
                '{"candidate_loop":"true"}',
                "invalid",
            ],
            strict=True,
        ):
            seed_change(conn, name, audit=audit)
        seed_change(conn, "other-document", audit_document="doc-absent")
        seed_change(conn, "old-draft")
        seed_change(conn, "old-ready", status="ready")
    candidate = tmp_path / "candidate.html"
    candidate.write_bytes(b"<h1>Retained draft</h1>")
    revisions = table_rows(path, "artifact_revisions")
    documents = table_rows(path, "artifact_documents")
    await retire(path, entrypoint)
    with sqlite3.connect(path) as conn:
        status = dict(conn.execute("SELECT change_set_id,status FROM artifact_change_sets"))
        assert all(status[f"change-{name}"] == "draft" for name in untouched)
        assert status["change-old-draft"] == status["change-old-ready"] == "failed"
        assert conn.execute(
            "SELECT COUNT(*) FROM artifact_mutation_attempts WHERE status='failed'"
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT COUNT(*) FROM artifact_audit_events "
            "WHERE event_type='change_set.legacy_html_retired'"
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT candidate_artifact_id,candidate_artifact_sha256 FROM artifact_change_sets "
            "WHERE change_set_id='change-old-draft'"
        ).fetchone() == ("candidate-old-draft", "b" * 64)
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='artifact_working_files'"
        ).fetchone()
    assert table_rows(path, "artifact_revisions") == revisions
    assert table_rows(path, "artifact_documents") == documents
    assert candidate.read_bytes() == b"<h1>Retained draft</h1>"
    before = {
        table: table_rows(path, table)
        for table in ["artifact_change_sets", "artifact_mutation_attempts", "artifact_audit_events"]
    }
    await retire(path, entrypoint)
    assert {table: table_rows(path, table) for table in before} == before


@pytest.mark.parametrize("entrypoint", ["runtime", "migration"])
async def test_only_verified_commits_resolve_ambiguous_receipts(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    path = tmp_path / "state.db"
    with database(path) as conn:
        seed_change(conn, "unknown", attempt_status="ambiguous")
        seed_change(conn, "confirmed", status="applied", attempt_status="ambiguous")
        seed_change(conn, "reserved-confirmed", status="applied")
        seed_change(
            conn,
            "wrong-content",
            status="applied",
            attempt_status="ambiguous",
            revision_matches=False,
        )
        seed_change(
            conn,
            "wrong-receipt",
            status="applied",
            attempt_status="ambiguous",
            receipt_matches=False,
        )
    before = table_rows(path, "artifact_mutation_attempts")
    await retire(path, entrypoint)
    with sqlite3.connect(path) as conn:
        statuses = dict(
            conn.execute("SELECT mutation_attempt_id,status FROM artifact_mutation_attempts")
        )
        assert statuses == {
            f"attempt-{name}": state
            for name, state in {
                "unknown": "ambiguous",
                "confirmed": "applied",
                "reserved-confirmed": "applied",
                "wrong-content": "ambiguous",
                "wrong-receipt": "ambiguous",
            }.items()
        }
        assert conn.execute(
            "SELECT change_set_id,revision_id,failure_code FROM artifact_mutation_attempts "
            "WHERE mutation_attempt_id='attempt-confirmed'"
        ).fetchone() == ("change-confirmed", "revision-confirmed", None)
    after = table_rows(path, "artifact_mutation_attempts")
    assert [
        row
        for row in after
        if row[0] in {"attempt-unknown", "attempt-wrong-content", "attempt-wrong-receipt"}
    ] == [
        row
        for row in before
        if row[0] in {"attempt-unknown", "attempt-wrong-content", "attempt-wrong-receipt"}
    ]


@pytest.mark.parametrize("entrypoint", ["runtime", "migration"])
async def test_retirement_closes_old_lifecycle_without_deleting_history(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    path = tmp_path / "state.db"
    with database(path) as conn:
        seed_change(conn, "ordinary", audit=None)
        conn.execute("""INSERT INTO artifact_edit_sessions (
            edit_session_id, document_id, base_revision_id, last_saved_revision_id,
            mode, status, user_id, state_revision, expires_at,
            last_access_at, created_at, updated_at
        ) VALUES ('editor', 'doc-ordinary', 'base-ordinary', 'base-ordinary', 'edit',
                  'active', 'user', 1, 9000000000000, 1, 1, 1)""")
        conn.execute("""INSERT INTO artifact_writer_leases (
            document_id, lease_id, holder_id, fencing_token, expires_at, created_at, updated_at
        ) VALUES ('doc-ordinary', 'lease', 'user', 9, 9000000000000, 1, 1)""")
        conn.commit()
    await retire(path, entrypoint)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT status,state_revision,last_saved_revision_id FROM artifact_edit_sessions"
        ).fetchone() == ("closed", 2, "base-ordinary")
        assert conn.execute(
            "SELECT expires_at,fencing_token FROM artifact_writer_leases"
        ).fetchone() == (0, 9)
        assert conn.execute("SELECT status FROM artifact_change_sets").fetchone() == ("draft",)
    before = table_rows(path, "artifact_edit_sessions"), table_rows(path, "artifact_writer_leases")
    await retire(path, entrypoint)
    assert (
        table_rows(path, "artifact_edit_sessions"),
        table_rows(path, "artifact_writer_leases"),
    ) == before


def test_v041_upgrades_existing_profile_and_keeps_forward_only_history(tmp_path: Path) -> None:
    path = tmp_path / "profile.db"
    backend = get_backend("sqlite:///" + str(path))
    try:
        with backend.lock():
            previous = read_migrations(str(MIGRATIONS)).filter(lambda item: item.id < MIGRATION_ID)
            backend.apply_migrations(backend.to_apply(previous))
    finally:
        backend.connection.close()
    with sqlite3.connect(path) as conn:
        seed_change(conn, "upgrade")
    assert apply_pending(str(path), MIGRATIONS) == [MIGRATION_ID]
    assert apply_pending(str(path), MIGRATIONS) == []
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT status FROM artifact_change_sets").fetchone() == ("failed",)
        assert conn.execute("SELECT COUNT(*) FROM artifact_revisions").fetchone() == (1,)
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='artifact_working_files'"
        ).fetchone()
