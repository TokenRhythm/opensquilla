"""V037 - durable artifact revisions, change sets, anchors, and audit state."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V036__session_model_routing"}

CREATE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS artifact_documents (
        document_id            TEXT PRIMARY KEY,
        session_key            TEXT NOT NULL,
        session_id             TEXT,
        name                   TEXT NOT NULL,
        kind                   TEXT NOT NULL,
        head_revision_id       TEXT NOT NULL,
        generation             INTEGER NOT NULL CHECK (generation >= 1),
        state_revision         INTEGER NOT NULL CHECK (state_revision >= 1),
        writer_fencing_token   INTEGER NOT NULL DEFAULT 0
                               CHECK (writer_fencing_token >= 0),
        created_at             INTEGER NOT NULL,
        updated_at             INTEGER NOT NULL,
        schema_version         INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifact_revisions (
        revision_id             TEXT PRIMARY KEY,
        document_id             TEXT NOT NULL,
        parent_revision_id      TEXT,
        generation              INTEGER NOT NULL CHECK (generation >= 1),
        artifact_id             TEXT NOT NULL,
        artifact_sha256         TEXT NOT NULL,
        filename                TEXT NOT NULL,
        media_type              TEXT NOT NULL,
        byte_size               INTEGER NOT NULL CHECK (byte_size >= 0),
        source                  TEXT NOT NULL,
        actor_kind              TEXT NOT NULL,
        actor_id                TEXT NOT NULL,
        change_set_id           TEXT,
        copied_from_revision_id TEXT,
        created_at              INTEGER NOT NULL,
        schema_version          INTEGER NOT NULL DEFAULT 1,
        UNIQUE (document_id, generation),
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (parent_revision_id) REFERENCES artifact_revisions(revision_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifact_change_sets (
        change_set_id             TEXT PRIMARY KEY,
        document_id               TEXT NOT NULL,
        base_revision_id          TEXT NOT NULL,
        turn_id                   TEXT,
        summary                   TEXT NOT NULL DEFAULT '',
        status                    TEXT NOT NULL,
        operations_json           TEXT NOT NULL,
        candidate_artifact_id     TEXT,
        candidate_artifact_sha256 TEXT,
        candidate_filename        TEXT,
        candidate_media_type      TEXT,
        candidate_byte_size       INTEGER CHECK (
            candidate_byte_size IS NULL OR candidate_byte_size >= 0
        ),
        validation_json           TEXT,
        state_revision            INTEGER NOT NULL CHECK (state_revision >= 1),
        created_by_kind           TEXT NOT NULL,
        created_by_id             TEXT NOT NULL,
        applied_revision_id       TEXT,
        created_at                INTEGER NOT NULL,
        updated_at                INTEGER NOT NULL,
        schema_version            INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (base_revision_id) REFERENCES artifact_revisions(revision_id),
        FOREIGN KEY (applied_revision_id) REFERENCES artifact_revisions(revision_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifact_anchors (
        anchor_id                TEXT PRIMARY KEY,
        document_id              TEXT NOT NULL,
        revision_id              TEXT NOT NULL,
        kind                     TEXT NOT NULL,
        locator_json             TEXT NOT NULL,
        quote                    TEXT,
        context_json             TEXT,
        state                    TEXT NOT NULL,
        remapped_from_anchor_id  TEXT,
        created_at               INTEGER NOT NULL,
        schema_version           INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (revision_id) REFERENCES artifact_revisions(revision_id),
        FOREIGN KEY (remapped_from_anchor_id) REFERENCES artifact_anchors(anchor_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifact_writer_leases (
        document_id    TEXT PRIMARY KEY,
        lease_id       TEXT NOT NULL UNIQUE,
        holder_id      TEXT NOT NULL,
        fencing_token  INTEGER NOT NULL CHECK (fencing_token >= 1),
        expires_at     INTEGER NOT NULL,
        created_at     INTEGER NOT NULL,
        updated_at     INTEGER NOT NULL,
        schema_version INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifact_edit_sessions (
        edit_session_id        TEXT PRIMARY KEY,
        document_id            TEXT NOT NULL,
        base_revision_id       TEXT NOT NULL,
        last_saved_revision_id TEXT NOT NULL,
        mode                   TEXT NOT NULL,
        status                 TEXT NOT NULL,
        user_id                TEXT NOT NULL,
        state_revision         INTEGER NOT NULL CHECK (state_revision >= 1),
        expires_at             INTEGER NOT NULL,
        last_access_at         INTEGER NOT NULL,
        created_at             INTEGER NOT NULL,
        updated_at             INTEGER NOT NULL,
        schema_version         INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (base_revision_id) REFERENCES artifact_revisions(revision_id),
        FOREIGN KEY (last_saved_revision_id) REFERENCES artifact_revisions(revision_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifact_audit_events (
        sequence        INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id        TEXT NOT NULL UNIQUE,
        document_id     TEXT NOT NULL,
        event_type      TEXT NOT NULL,
        actor_kind      TEXT NOT NULL,
        actor_id        TEXT NOT NULL,
        revision_id     TEXT,
        change_set_id   TEXT,
        anchor_id       TEXT,
        edit_session_id TEXT,
        lease_id        TEXT,
        payload_json    TEXT NOT NULL,
        created_at      INTEGER NOT NULL,
        schema_version  INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_artifact_documents_session
    ON artifact_documents(session_key, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_artifact_revisions_document
    ON artifact_revisions(document_id, generation DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_artifact_revisions_artifact
    ON artifact_revisions(artifact_id, document_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_artifact_change_sets_document_status
    ON artifact_change_sets(document_id, status, updated_at DESC)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_change_sets_turn
    ON artifact_change_sets(turn_id)
    WHERE turn_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_artifact_anchors_revision
    ON artifact_anchors(document_id, revision_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_artifact_edit_sessions_document_status
    ON artifact_edit_sessions(document_id, status, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_artifact_edit_sessions_expiry
    ON artifact_edit_sessions(status, expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_artifact_writer_leases_expiry
    ON artifact_writer_leases(expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_artifact_audit_document_sequence
    ON artifact_audit_events(document_id, sequence)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS artifact_revisions_immutable
    BEFORE UPDATE ON artifact_revisions
    BEGIN
        SELECT RAISE(ABORT, 'artifact revisions are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS artifact_anchors_immutable
    BEFORE UPDATE ON artifact_anchors
    BEGIN
        SELECT RAISE(ABORT, 'artifact anchors are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS artifact_audit_events_immutable
    BEFORE UPDATE ON artifact_audit_events
    BEGIN
        SELECT RAISE(ABORT, 'artifact audit events are immutable');
    END
    """,
)

DROP_STATEMENTS: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS artifact_audit_events_immutable",
    "DROP TRIGGER IF EXISTS artifact_anchors_immutable",
    "DROP TRIGGER IF EXISTS artifact_revisions_immutable",
    "DROP INDEX IF EXISTS idx_artifact_audit_document_sequence",
    "DROP INDEX IF EXISTS idx_artifact_writer_leases_expiry",
    "DROP INDEX IF EXISTS idx_artifact_edit_sessions_expiry",
    "DROP INDEX IF EXISTS idx_artifact_edit_sessions_document_status",
    "DROP INDEX IF EXISTS idx_artifact_anchors_revision",
    "DROP INDEX IF EXISTS idx_artifact_change_sets_turn",
    "DROP INDEX IF EXISTS idx_artifact_change_sets_document_status",
    "DROP INDEX IF EXISTS idx_artifact_revisions_artifact",
    "DROP INDEX IF EXISTS idx_artifact_revisions_document",
    "DROP INDEX IF EXISTS idx_artifact_documents_session",
    "DROP TABLE IF EXISTS artifact_audit_events",
    "DROP TABLE IF EXISTS artifact_edit_sessions",
    "DROP TABLE IF EXISTS artifact_writer_leases",
    "DROP TABLE IF EXISTS artifact_anchors",
    "DROP TABLE IF EXISTS artifact_change_sets",
    "DROP TABLE IF EXISTS artifact_revisions",
    "DROP TABLE IF EXISTS artifact_documents",
)


def apply_step(conn) -> None:
    cur = conn.cursor()
    for statement in CREATE_STATEMENTS:
        cur.execute(statement)


def rollback_step(conn) -> None:
    cur = conn.cursor()
    for statement in DROP_STATEMENTS:
        cur.execute(statement)


steps = [step(apply_step, rollback_step)]
