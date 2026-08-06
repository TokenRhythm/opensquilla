"""Upgrade coverage for the durable MetaSkill launch outbox."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from opensquilla.persistence.migrator import apply_pending
from opensquilla.session.storage import SessionStorage

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
V031_ID = "V031__meta_launch_drafts"


@pytest.mark.asyncio
async def test_v026_preserves_runtime_created_table_and_adds_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    try:
        draft, _ = await storage.stage_meta_launch_draft(
            session_key="agent:main:webchat:pre-migration",
            client_request_id="pre-migration-request",
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- Preserve this exact request",
        )
    finally:
        await storage.close()

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DROP INDEX uq_meta_launch_drafts_request")
        connection.execute("DROP INDEX idx_meta_launch_drafts_session_expiry")
        connection.commit()
    finally:
        connection.close()

    assert V031_ID in apply_pending(str(db_path), MIGRATIONS_DIR)

    connection = sqlite3.connect(db_path)
    try:
        preserved = connection.execute(
            "SELECT launch_text FROM meta_launch_drafts WHERE draft_id = ?",
            (draft.draft_id,),
        ).fetchone()
        indexes = {
            str(row[1]): bool(row[2])
            for row in connection.execute("PRAGMA index_list(meta_launch_drafts)")
        }
    finally:
        connection.close()

    assert preserved == ("/meta meta-paper-write -- Preserve this exact request",)
    assert indexes["uq_meta_launch_drafts_request"] is True
    assert indexes["idx_meta_launch_drafts_session_expiry"] is False


def test_v026_applies_additive_schema_on_migrated_database(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    assert V031_ID in apply_pending(str(db_path), MIGRATIONS_DIR)

    connection = sqlite3.connect(db_path)
    try:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(meta_launch_drafts)")
        }
        assert {
            "draft_id",
            "session_key",
            "client_request_id",
            "meta_skill_name",
            "launch_text",
            "created_at",
            "updated_at",
            "expires_at",
        } <= columns
    finally:
        connection.close()
