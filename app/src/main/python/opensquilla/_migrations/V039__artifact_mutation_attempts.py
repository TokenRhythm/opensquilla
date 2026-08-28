"""V039 - durable idempotency receipts for artifact-writing tool calls."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V038__artifact_prompt_annotations"}

CREATE_STATEMENTS: tuple[str, ...] = (
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
    CREATE INDEX IF NOT EXISTS idx_artifact_mutation_attempts_document_status
    ON artifact_mutation_attempts(document_id, status, updated_at DESC)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_mutation_attempts_candidate
    ON artifact_mutation_attempts(candidate_session_id, candidate_artifact_id)
    WHERE candidate_artifact_id IS NOT NULL
    """,
)

DROP_STATEMENTS: tuple[str, ...] = (
    "DROP INDEX IF EXISTS idx_artifact_mutation_attempts_document_status",
    "DROP INDEX IF EXISTS idx_artifact_mutation_attempts_candidate",
    "DROP TABLE IF EXISTS artifact_mutation_attempts",
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
