"""SQLite schema shared by ArtifactSession runtime initialization and migration tests."""

from __future__ import annotations

SCHEMA_STATEMENTS: tuple[str, ...] = (
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
        anchor_id                 TEXT PRIMARY KEY,
        document_id               TEXT NOT NULL,
        revision_id               TEXT NOT NULL,
        kind                      TEXT NOT NULL,
        locator_json              TEXT NOT NULL,
        quote                     TEXT,
        context_json              TEXT,
        state                     TEXT NOT NULL,
        remapped_from_anchor_id   TEXT,
        created_at                INTEGER NOT NULL,
        schema_version            INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (revision_id) REFERENCES artifact_revisions(revision_id),
        FOREIGN KEY (remapped_from_anchor_id) REFERENCES artifact_anchors(anchor_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifact_prompt_annotations (
        annotation_id   TEXT PRIMARY KEY,
        session_key     TEXT NOT NULL,
        session_id      TEXT NOT NULL,
        session_epoch   INTEGER NOT NULL CHECK (session_epoch >= 0),
        document_id     TEXT NOT NULL,
        revision_id     TEXT NOT NULL,
        anchor_id       TEXT NOT NULL,
        body            TEXT NOT NULL
                        CHECK (length(CAST(body AS BLOB)) <= 16384),
        status          TEXT NOT NULL
                        CHECK (status IN ('draft', 'sent', 'discarded')),
        state_revision  INTEGER NOT NULL CHECK (state_revision >= 1),
        sent_message_id TEXT,
        sent_turn_id    TEXT,
        sent_order      INTEGER CHECK (sent_order IS NULL OR sent_order >= 0),
        created_at      INTEGER NOT NULL,
        updated_at      INTEGER NOT NULL,
        schema_version  INTEGER NOT NULL DEFAULT 1,
        CHECK (
            (status = 'sent' AND sent_message_id IS NOT NULL
             AND sent_turn_id IS NOT NULL AND sent_order IS NOT NULL)
            OR
            (status IN ('draft', 'discarded') AND sent_message_id IS NULL
             AND sent_turn_id IS NULL AND sent_order IS NULL)
        ),
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (revision_id) REFERENCES artifact_revisions(revision_id),
        FOREIGN KEY (anchor_id) REFERENCES artifact_anchors(anchor_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifact_mutation_attempts (
        mutation_attempt_id TEXT PRIMARY KEY,
        document_id         TEXT NOT NULL,
        turn_id             TEXT NOT NULL,
        tool_use_id         TEXT NOT NULL,
        base_revision_id    TEXT NOT NULL,
        proposal_sha256     TEXT
                            CHECK (
                                proposal_sha256 IS NULL
                                OR (
                                    length(proposal_sha256) = 64
                                    AND proposal_sha256 = lower(proposal_sha256)
                                    AND proposal_sha256 NOT GLOB '*[^0-9a-f]*'
                                )
                            ),
        status              TEXT NOT NULL
                            CHECK (status IN ('reserved', 'applied', 'failed', 'ambiguous')),
        change_set_id       TEXT,
        revision_id         TEXT,
        failure_code        TEXT
                            CHECK (
                                failure_code IS NULL
                                OR length(CAST(failure_code AS BLOB)) <= 128
                            ),
        candidate_session_id      TEXT,
        candidate_artifact_id     TEXT,
        candidate_artifact_sha256 TEXT,
        candidate_registered_at   INTEGER,
        state_revision      INTEGER NOT NULL CHECK (state_revision >= 1),
        created_at          INTEGER NOT NULL,
        updated_at          INTEGER NOT NULL,
        schema_version      INTEGER NOT NULL DEFAULT 1,
        UNIQUE (turn_id),
        CHECK (
            (status = 'reserved' AND change_set_id IS NULL
             AND revision_id IS NULL AND failure_code IS NULL)
            OR
            (status = 'applied' AND change_set_id IS NOT NULL
             AND revision_id IS NOT NULL AND failure_code IS NULL)
            OR
            (status IN ('failed', 'ambiguous') AND failure_code IS NOT NULL)
        ),
        CHECK (
            (candidate_session_id IS NULL AND candidate_artifact_id IS NULL
             AND candidate_artifact_sha256 IS NULL AND candidate_registered_at IS NULL)
            OR
            (candidate_session_id IS NOT NULL AND candidate_artifact_id IS NOT NULL
             AND candidate_artifact_sha256 IS NOT NULL
             AND candidate_registered_at IS NOT NULL)
        ),
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (base_revision_id) REFERENCES artifact_revisions(revision_id),
        FOREIGN KEY (change_set_id) REFERENCES artifact_change_sets(change_set_id),
        FOREIGN KEY (revision_id) REFERENCES artifact_revisions(revision_id)
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
        edit_session_id       TEXT PRIMARY KEY,
        document_id           TEXT NOT NULL,
        base_revision_id      TEXT NOT NULL,
        last_saved_revision_id TEXT NOT NULL,
        mode                  TEXT NOT NULL,
        status                TEXT NOT NULL,
        user_id               TEXT NOT NULL,
        state_revision        INTEGER NOT NULL CHECK (state_revision >= 1),
        expires_at            INTEGER NOT NULL,
        last_access_at        INTEGER NOT NULL,
        created_at            INTEGER NOT NULL,
        updated_at            INTEGER NOT NULL,
        schema_version        INTEGER NOT NULL DEFAULT 1,
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
    CREATE INDEX IF NOT EXISTS idx_artifact_prompt_annotations_session_status
    ON artifact_prompt_annotations(
        session_key, session_id, session_epoch, status, created_at, annotation_id
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_artifact_prompt_annotations_document_revision
    ON artifact_prompt_annotations(document_id, revision_id, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_artifact_mutation_attempts_document_status
    ON artifact_mutation_attempts(document_id, status, updated_at DESC)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_mutation_attempts_candidate
    ON artifact_mutation_attempts(candidate_session_id, candidate_artifact_id)
    WHERE candidate_artifact_id IS NOT NULL
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
    """
    CREATE TABLE IF NOT EXISTS document_source_bindings (
        binding_id         TEXT PRIMARY KEY,
        document_id        TEXT NOT NULL UNIQUE,
        session_key        TEXT NOT NULL,
        session_id         TEXT NOT NULL,
        source_type        TEXT NOT NULL CHECK (source_type IN ('attachment', 'deliverable')),
        source_resource_id TEXT NOT NULL,
        source_sha256      TEXT NOT NULL CHECK (length(source_sha256) = 64),
        source_name        TEXT NOT NULL,
        source_mime        TEXT NOT NULL,
        source_size        INTEGER NOT NULL CHECK (source_size >= 0),
        mode               TEXT NOT NULL CHECK (mode = 'copy'),
        created_at         INTEGER NOT NULL,
        schema_version     INTEGER NOT NULL DEFAULT 1,
        UNIQUE (session_id, source_type, source_resource_id),
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document_import_attempts (
        attempt_id            TEXT PRIMARY KEY,
        session_key           TEXT NOT NULL,
        session_id            TEXT NOT NULL,
        idempotency_key       TEXT NOT NULL,
        source_type           TEXT NOT NULL CHECK (source_type IN ('attachment', 'deliverable')),
        source_resource_id    TEXT NOT NULL,
        source_sha256         TEXT NOT NULL CHECK (length(source_sha256) = 64),
        source_name           TEXT NOT NULL,
        source_mime           TEXT NOT NULL,
        source_size           INTEGER NOT NULL CHECK (source_size >= 0),
        document_name         TEXT NOT NULL,
        mode                  TEXT NOT NULL CHECK (mode = 'copy'),
        candidate_artifact_id TEXT NOT NULL UNIQUE,
        status                TEXT NOT NULL
                              CHECK (status IN ('reserved', 'applied', 'failed', 'ambiguous')),
        document_id           TEXT,
        revision_id           TEXT,
        binding_id            TEXT,
        failure_code          TEXT
                              CHECK (failure_code IS NULL OR length(failure_code) <= 128),
        candidate_cleaned_at  INTEGER,
        state_revision        INTEGER NOT NULL CHECK (state_revision >= 1),
        created_at            INTEGER NOT NULL,
        updated_at            INTEGER NOT NULL,
        schema_version        INTEGER NOT NULL DEFAULT 1,
        UNIQUE (session_id, idempotency_key),
        CHECK (
            (status = 'reserved' AND document_id IS NULL AND revision_id IS NULL
             AND binding_id IS NULL AND failure_code IS NULL)
            OR
            (status = 'applied' AND document_id IS NOT NULL AND revision_id IS NOT NULL
             AND binding_id IS NOT NULL AND failure_code IS NULL)
            OR
            (status IN ('failed', 'ambiguous') AND failure_code IS NOT NULL)
        ),
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (revision_id) REFERENCES artifact_revisions(revision_id),
        FOREIGN KEY (binding_id) REFERENCES document_source_bindings(binding_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document_publications (
        publication_id          TEXT PRIMARY KEY,
        session_key             TEXT NOT NULL,
        session_id              TEXT NOT NULL,
        document_id             TEXT NOT NULL,
        revision_id             TEXT NOT NULL,
        deliverable_artifact_id TEXT NOT NULL UNIQUE,
        artifact_sha256         TEXT NOT NULL CHECK (length(artifact_sha256) = 64),
        name                    TEXT NOT NULL,
        mime                    TEXT NOT NULL,
        size                    INTEGER NOT NULL CHECK (size >= 0),
        created_by_kind         TEXT NOT NULL,
        created_by_id           TEXT NOT NULL,
        created_at              INTEGER NOT NULL,
        schema_version          INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (revision_id) REFERENCES artifact_revisions(revision_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document_publish_attempts (
        attempt_id              TEXT PRIMARY KEY,
        session_key             TEXT NOT NULL,
        session_id              TEXT NOT NULL,
        idempotency_key         TEXT NOT NULL,
        document_id             TEXT NOT NULL,
        revision_id             TEXT NOT NULL,
        candidate_artifact_id   TEXT NOT NULL UNIQUE,
        artifact_sha256         TEXT NOT NULL CHECK (length(artifact_sha256) = 64),
        name                    TEXT NOT NULL,
        mime                    TEXT NOT NULL,
        size                    INTEGER NOT NULL CHECK (size >= 0),
        status                  TEXT NOT NULL
                                CHECK (status IN ('reserved', 'applied', 'failed', 'ambiguous')),
        publication_id          TEXT,
        deliverable_artifact_id TEXT,
        failure_code            TEXT
                                CHECK (failure_code IS NULL OR length(failure_code) <= 128),
        promoted_at             INTEGER,
        state_revision          INTEGER NOT NULL CHECK (state_revision >= 1),
        created_at              INTEGER NOT NULL,
        updated_at              INTEGER NOT NULL,
        schema_version          INTEGER NOT NULL DEFAULT 1,
        UNIQUE (session_id, idempotency_key),
        CHECK (
            (status = 'reserved' AND publication_id IS NULL
             AND deliverable_artifact_id IS NULL AND failure_code IS NULL)
            OR
            (status = 'applied' AND publication_id IS NOT NULL
             AND deliverable_artifact_id IS NOT NULL AND failure_code IS NULL)
            OR
            (status IN ('failed', 'ambiguous') AND failure_code IS NOT NULL)
        ),
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (revision_id) REFERENCES artifact_revisions(revision_id),
        FOREIGN KEY (publication_id) REFERENCES document_publications(publication_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_document_source_bindings_session
    ON document_source_bindings(session_id, source_type, source_resource_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_document_import_attempts_status
    ON document_import_attempts(session_id, status, updated_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_document_publications_session
    ON document_publications(session_id, created_at DESC, publication_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_document_publications_document
    ON document_publications(document_id, created_at DESC, publication_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_document_publish_attempts_status
    ON document_publish_attempts(session_id, status, updated_at)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS document_source_bindings_immutable
    BEFORE UPDATE ON document_source_bindings
    BEGIN
        SELECT RAISE(ABORT, 'document source bindings are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS document_publications_immutable
    BEFORE UPDATE ON document_publications
    BEGIN
        SELECT RAISE(ABORT, 'document publications are immutable');
    END
    """,
)


SCHEMA_OBJECTS: tuple[str, ...] = (
    "document_publications_immutable",
    "document_source_bindings_immutable",
    "idx_document_publish_attempts_status",
    "idx_document_publications_document",
    "idx_document_publications_session",
    "idx_document_import_attempts_status",
    "idx_document_source_bindings_session",
    "document_publish_attempts",
    "document_publications",
    "document_import_attempts",
    "document_source_bindings",
    "artifact_audit_events_immutable",
    "artifact_anchors_immutable",
    "artifact_revisions_immutable",
    "idx_artifact_audit_document_sequence",
    "idx_artifact_writer_leases_expiry",
    "idx_artifact_edit_sessions_expiry",
    "idx_artifact_edit_sessions_document_status",
    "idx_artifact_prompt_annotations_document_revision",
    "idx_artifact_prompt_annotations_session_status",
    "idx_artifact_mutation_attempts_document_status",
    "idx_artifact_mutation_attempts_candidate",
    "idx_artifact_anchors_revision",
    "idx_artifact_change_sets_turn",
    "idx_artifact_change_sets_document_status",
    "idx_artifact_revisions_artifact",
    "idx_artifact_revisions_document",
    "idx_artifact_documents_session",
    "artifact_audit_events",
    "artifact_edit_sessions",
    "artifact_writer_leases",
    "artifact_prompt_annotations",
    "artifact_mutation_attempts",
    "artifact_anchors",
    "artifact_change_sets",
    "artifact_revisions",
    "artifact_documents",
)
