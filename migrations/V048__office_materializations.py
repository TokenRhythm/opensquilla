"""V048 - durable receipts for atomic Office working-file replacement."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V047__retire_product_modes", "V041__retire_html_editor"}

CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS artifact_office_materializations (
        document_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE,
        session_key TEXT NOT NULL,
        session_id TEXT NOT NULL,
        workspace TEXT NOT NULL,
        expected_revision_id TEXT NOT NULL,
        plan_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('prepared', 'applied')),
        result_revision_id TEXT,
        FOREIGN KEY (document_id) REFERENCES artifact_documents(document_id) ON DELETE CASCADE
    )
"""


def rollback_step(conn) -> None:
    if conn.execute("SELECT 1 FROM artifact_office_materializations LIMIT 1").fetchone():
        raise RuntimeError("Resolve pending Office materializations before downgrading")
    conn.execute("DROP TABLE IF EXISTS artifact_office_materializations")


steps = [step(CREATE_TABLE, rollback_step)]
