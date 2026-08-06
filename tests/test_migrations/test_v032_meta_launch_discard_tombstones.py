"""Upgrade coverage for terminal MetaSkill draft cancellation markers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from opensquilla.persistence.migrator import apply_pending
from opensquilla.session.storage import SessionStorage

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
V032_ID = "V032__meta_launch_discard_tombstones"


@pytest.mark.asyncio
async def test_v027_preserves_runtime_created_marker_and_adds_index(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    try:
        await storage.stage_meta_launch_draft(
            session_key="agent:main:webchat:pre-v027",
            client_request_id="pre-v027-request",
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- Never retain this prompt in the marker",
        )
        assert await storage.discard_meta_launch_draft(
            session_key="agent:main:webchat:pre-v027",
            client_request_id="pre-v027-request",
        )
    finally:
        await storage.close()

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DROP INDEX idx_meta_launch_discard_tombstones_expiry")
        connection.commit()
    finally:
        connection.close()

    assert V032_ID in apply_pending(str(db_path), MIGRATIONS_DIR)

    connection = sqlite3.connect(db_path)
    try:
        preserved = connection.execute(
            """
            SELECT session_key, client_request_id
            FROM meta_launch_discard_tombstones
            """
        ).fetchall()
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(meta_launch_discard_tombstones)"
            )
        }
        indexes = {
            str(row[1]): bool(row[2])
            for row in connection.execute(
                "PRAGMA index_list(meta_launch_discard_tombstones)"
            )
        }
    finally:
        connection.close()

    assert preserved == [("agent:main:webchat:pre-v027", "pre-v027-request")]
    assert "launch_text" not in columns
    assert "meta_skill_name" not in columns
    assert indexes["idx_meta_launch_discard_tombstones_expiry"] is False


def test_v027_applies_additive_content_free_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    assert V032_ID in apply_pending(str(db_path), MIGRATIONS_DIR)

    connection = sqlite3.connect(db_path)
    try:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(meta_launch_discard_tombstones)"
            )
        }
    finally:
        connection.close()

    assert columns == {
        "session_key",
        "client_request_id",
        "created_at",
        "expires_at",
        "schema_version",
    }
