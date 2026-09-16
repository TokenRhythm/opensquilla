"""V041 - retire discontinued HTML editing sessions while preserving artifact history."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V039__artifact_mutation_attempts", "V040__document_resources"}

RETIREMENT_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS artifact_working_files (
        document_id TEXT PRIMARY KEY REFERENCES artifact_documents(document_id) ON DELETE CASCADE,
        workspace TEXT NOT NULL,
        relative_root TEXT NOT NULL,
        entrypoint TEXT NOT NULL,
        base_revision_id TEXT NOT NULL REFERENCES artifact_revisions(revision_id)
    )
    """,
    """
    INSERT OR IGNORE INTO artifact_audit_events (
            event_id, document_id, event_type, actor_kind, actor_id,
            change_set_id, payload_json, created_at
        )
        SELECT 'legacy-html-retired:' || change_set_id, document_id,
               'change_set.legacy_html_retired', 'system', 'html-editor-retirement',
               change_set_id, '{"candidate_bytes_retained":true}',
               CAST(strftime('%s', 'now') AS INTEGER) * 1000
        FROM artifact_change_sets
        WHERE change_set_id IN (
        SELECT change_set.change_set_id
        FROM artifact_change_sets AS change_set
        WHERE EXISTS (
            SELECT 1 FROM artifact_audit_events AS audit
            WHERE audit.change_set_id = change_set.change_set_id
              AND audit.document_id = change_set.document_id
              AND audit.event_type = 'change_set.created'
              AND json_type(
                  CASE WHEN json_valid(audit.payload_json) THEN audit.payload_json ELSE '{}' END,
                  '$.candidate_loop'
              ) = 'true'
        )
    )
    """,
    """
    UPDATE artifact_mutation_attempts AS attempt
        SET status = 'applied', failure_code = NULL,
            change_set_id = (
                SELECT change_set_id FROM artifact_change_sets
                WHERE document_id = attempt.document_id AND turn_id = attempt.turn_id
            ),
            revision_id = (
                SELECT applied_revision_id FROM artifact_change_sets
                WHERE document_id = attempt.document_id AND turn_id = attempt.turn_id
            ),
            state_revision = state_revision + 1,
            updated_at = MAX(updated_at, CAST(strftime('%s', 'now') AS INTEGER) * 1000)
        WHERE status IN ('reserved', 'ambiguous') AND EXISTS (
            SELECT 1 FROM artifact_change_sets AS change_set
            JOIN artifact_revisions AS revision
              ON revision.revision_id = change_set.applied_revision_id
            WHERE change_set.change_set_id IN (
        SELECT change_set.change_set_id
        FROM artifact_change_sets AS change_set
        WHERE EXISTS (
            SELECT 1 FROM artifact_audit_events AS audit
            WHERE audit.change_set_id = change_set.change_set_id
              AND audit.document_id = change_set.document_id
              AND audit.event_type = 'change_set.created'
              AND json_type(
                  CASE WHEN json_valid(audit.payload_json) THEN audit.payload_json ELSE '{}' END,
                  '$.candidate_loop'
              ) = 'true'
        )
    )
              AND change_set.document_id = attempt.document_id
              AND change_set.turn_id = attempt.turn_id
              AND change_set.base_revision_id = attempt.base_revision_id
              AND change_set.status = 'applied'
              AND revision.document_id = change_set.document_id
          AND revision.parent_revision_id = change_set.base_revision_id
              AND revision.change_set_id = change_set.change_set_id
              AND revision.artifact_id = change_set.candidate_artifact_id
              AND revision.artifact_sha256 = change_set.candidate_artifact_sha256
              AND (attempt.candidate_artifact_id IS NULL OR (
                  attempt.candidate_artifact_id = revision.artifact_id
                  AND attempt.candidate_artifact_sha256 = revision.artifact_sha256
              ))
        )
    """,
    """
    UPDATE artifact_change_sets
        SET status = 'failed', state_revision = state_revision + 1,
            updated_at = MAX(updated_at, CAST(strftime('%s', 'now') AS INTEGER) * 1000)
        WHERE status IN ('draft', 'ready')
          AND change_set_id IN (
        SELECT change_set.change_set_id
        FROM artifact_change_sets AS change_set
        WHERE EXISTS (
            SELECT 1 FROM artifact_audit_events AS audit
            WHERE audit.change_set_id = change_set.change_set_id
              AND audit.document_id = change_set.document_id
              AND audit.event_type = 'change_set.created'
              AND json_type(
                  CASE WHEN json_valid(audit.payload_json) THEN audit.payload_json ELSE '{}' END,
                  '$.candidate_loop'
              ) = 'true'
        )
    )
    """,
    """
    UPDATE artifact_mutation_attempts AS attempt
        SET status = 'failed', failure_code = 'LEGACY_HTML_EDITOR_RETIRED',
            state_revision = state_revision + 1,
            updated_at = MAX(updated_at, CAST(strftime('%s', 'now') AS INTEGER) * 1000)
        WHERE status = 'reserved' AND EXISTS (
            SELECT 1 FROM artifact_change_sets AS change_set
            WHERE change_set.change_set_id IN (
        SELECT change_set.change_set_id
        FROM artifact_change_sets AS change_set
        WHERE EXISTS (
            SELECT 1 FROM artifact_audit_events AS audit
            WHERE audit.change_set_id = change_set.change_set_id
              AND audit.document_id = change_set.document_id
              AND audit.event_type = 'change_set.created'
              AND json_type(
                  CASE WHEN json_valid(audit.payload_json) THEN audit.payload_json ELSE '{}' END,
                  '$.candidate_loop'
              ) = 'true'
        )
    )
              AND change_set.document_id = attempt.document_id
              AND change_set.turn_id = attempt.turn_id
              AND change_set.base_revision_id = attempt.base_revision_id
              AND change_set.status IN ('failed', 'rejected', 'conflict')
        )
    """,
    """
    UPDATE artifact_edit_sessions
    SET status = 'closed', state_revision = state_revision + 1,
        expires_at = MIN(expires_at, CAST(strftime('%s', 'now') AS INTEGER) * 1000),
        updated_at = MAX(updated_at, CAST(strftime('%s', 'now') AS INTEGER) * 1000)
    WHERE status = 'active'
    """,
    """
    UPDATE artifact_writer_leases
    SET expires_at = 0,
        updated_at = MAX(updated_at, CAST(strftime('%s', 'now') AS INTEGER) * 1000)
    WHERE expires_at > 0
    """,
)


def apply_step(conn) -> None:
    for statement in RETIREMENT_STATEMENTS:
        conn.execute(statement)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS artifact_working_sources (
            document_id TEXT PRIMARY KEY REFERENCES artifact_working_files(document_id)
                ON DELETE CASCADE,
            session_key TEXT NOT NULL,
            session_id TEXT NOT NULL,
            workspace TEXT NOT NULL,
            source_path TEXT NOT NULL,
            bundle_mode TEXT NOT NULL CHECK(bundle_mode IN ('auto', 'none', 'directory')),
            bundle_root TEXT,
            UNIQUE(session_key, session_id, workspace, source_path)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS artifact_working_source_versions (
            revision_id TEXT PRIMARY KEY REFERENCES artifact_revisions(revision_id)
                ON DELETE CASCADE,
            document_id TEXT NOT NULL REFERENCES artifact_working_files(document_id)
                ON DELETE CASCADE,
            relative_root TEXT NOT NULL,
            entrypoint TEXT NOT NULL,
            bundle_mode TEXT NOT NULL CHECK(bundle_mode IN ('auto', 'none', 'directory')),
            bundle_root TEXT
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO artifact_working_source_versions
        SELECT working.base_revision_id, working.document_id, working.relative_root,
               working.entrypoint, source.bundle_mode, source.bundle_root
        FROM artifact_working_files AS working
        JOIN artifact_working_sources AS source USING(document_id)
    """)


def rollback_step(conn) -> None:
    # Rolling back cannot safely reactivate discontinued sessions or delete retained work.
    pass


steps = [step(apply_step, rollback_step)]
