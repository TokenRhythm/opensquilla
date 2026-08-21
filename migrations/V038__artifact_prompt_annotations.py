"""V038 - durable prompt-annotation drafts bound to artifact revisions."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V037__artifact_sessions"}

CREATE_STATEMENTS: tuple[str, ...] = (
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
    CREATE INDEX IF NOT EXISTS idx_artifact_prompt_annotations_session_status
    ON artifact_prompt_annotations(
        session_key, session_id, session_epoch, status, created_at, annotation_id
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_artifact_prompt_annotations_document_revision
    ON artifact_prompt_annotations(document_id, revision_id, status)
    """,
)

DROP_STATEMENTS: tuple[str, ...] = (
    "DROP INDEX IF EXISTS idx_artifact_prompt_annotations_document_revision",
    "DROP INDEX IF EXISTS idx_artifact_prompt_annotations_session_status",
    "DROP TABLE IF EXISTS artifact_prompt_annotations",
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
