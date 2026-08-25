"""V040 - document source, import, and immutable publication receipts."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V039__artifact_mutation_attempts"}

CREATE_STATEMENTS: tuple[str, ...] = (
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
        publication_id         TEXT PRIMARY KEY,
        session_key            TEXT NOT NULL,
        session_id             TEXT NOT NULL,
        document_id            TEXT NOT NULL,
        revision_id            TEXT NOT NULL,
        deliverable_artifact_id TEXT NOT NULL UNIQUE,
        artifact_sha256        TEXT NOT NULL CHECK (length(artifact_sha256) = 64),
        name                   TEXT NOT NULL,
        mime                   TEXT NOT NULL,
        size                   INTEGER NOT NULL CHECK (size >= 0),
        created_by_kind        TEXT NOT NULL,
        created_by_id          TEXT NOT NULL,
        created_at             INTEGER NOT NULL,
        schema_version         INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id)
            ON DELETE CASCADE,
        FOREIGN KEY (revision_id) REFERENCES artifact_revisions(revision_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document_publish_attempts (
        attempt_id             TEXT PRIMARY KEY,
        session_key            TEXT NOT NULL,
        session_id             TEXT NOT NULL,
        idempotency_key        TEXT NOT NULL,
        document_id            TEXT NOT NULL,
        revision_id            TEXT NOT NULL,
        candidate_artifact_id  TEXT NOT NULL UNIQUE,
        artifact_sha256        TEXT NOT NULL CHECK (length(artifact_sha256) = 64),
        name                   TEXT NOT NULL,
        mime                   TEXT NOT NULL,
        size                   INTEGER NOT NULL CHECK (size >= 0),
        status                 TEXT NOT NULL
                               CHECK (status IN ('reserved', 'applied', 'failed', 'ambiguous')),
        publication_id         TEXT,
        deliverable_artifact_id TEXT,
        failure_code           TEXT
                               CHECK (failure_code IS NULL OR length(failure_code) <= 128),
        promoted_at            INTEGER,
        state_revision         INTEGER NOT NULL CHECK (state_revision >= 1),
        created_at             INTEGER NOT NULL,
        updated_at             INTEGER NOT NULL,
        schema_version         INTEGER NOT NULL DEFAULT 1,
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

DROP_STATEMENTS: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS document_publications_immutable",
    "DROP TRIGGER IF EXISTS document_source_bindings_immutable",
    "DROP INDEX IF EXISTS idx_document_publish_attempts_status",
    "DROP INDEX IF EXISTS idx_document_publications_document",
    "DROP INDEX IF EXISTS idx_document_publications_session",
    "DROP INDEX IF EXISTS idx_document_import_attempts_status",
    "DROP INDEX IF EXISTS idx_document_source_bindings_session",
    "DROP TABLE IF EXISTS document_publish_attempts",
    "DROP TABLE IF EXISTS document_publications",
    "DROP TABLE IF EXISTS document_import_attempts",
    "DROP TABLE IF EXISTS document_source_bindings",
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
