"""Regression coverage for the additive durable MetaSkill-control ledger."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import apply_pending
from opensquilla.session.storage import SessionStorage

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
V030_ID = "V030__meta_control_intents"


@pytest.mark.asyncio
async def test_v025_accepts_compatible_table_created_before_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    try:
        intent, _ = await storage.stage_meta_control_intent(
            session_key="agent:main:webchat:pre-migration",
            control_kind="manual",
            correlation_id="request:pre-migration",
            meta_skill_name="meta-paper-write",
        )
    finally:
        await storage.close()

    # Model an out-of-band compatible table as well as the runtime-created
    # upgrade path: V025 must preserve rows and add any missing guarantees.
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("DROP INDEX uq_meta_control_intents_correlation")
        connection.execute("DROP INDEX idx_meta_control_intents_session_status")
        connection.commit()
    finally:
        connection.close()

    applied = apply_pending(str(db_path), MIGRATIONS_DIR)

    assert V030_ID in applied
    connection = sqlite3.connect(db_path)
    try:
        preserved = connection.execute(
            "SELECT meta_skill_name FROM meta_control_intents WHERE intent_id = ?",
            (intent.intent_id,),
        ).fetchone()
        indexes = {
            str(row[1]): bool(row[2])
            for row in connection.execute("PRAGMA index_list(meta_control_intents)")
        }
    finally:
        connection.close()

    assert preserved == ("meta-paper-write",)
    assert indexes["uq_meta_control_intents_correlation"] is True
    assert indexes["idx_meta_control_intents_session_status"] is False


def test_v025_applies_constraints_indexes_and_rolls_back(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    applied = apply_pending(str(db_path), MIGRATIONS_DIR)
    assert V030_ID in applied

    connection = sqlite3.connect(db_path)
    try:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(meta_control_intents)")
        }
        assert {
            "intent_id",
            "session_key",
            "control_kind",
            "correlation_id",
            "meta_skill_name",
            "replay_run_id",
            "replay_mode",
            "status",
            "accepted_request_fingerprint",
            "accepted_message_id",
            "accepted_task_id",
        } <= columns
        indexes = {
            str(row[1]): bool(row[2])
            for row in connection.execute("PRAGMA index_list(meta_control_intents)")
        }
        assert indexes["uq_meta_control_intents_correlation"] is True
        assert indexes["idx_meta_control_intents_session_status"] is False

        row = (
            "intent-1",
            "agent:main:test",
            "manual",
            "request:req-1",
            "meta-paper-write",
            "staged",
            1,
            1,
        )
        connection.execute(
            """
            INSERT INTO meta_control_intents (
                intent_id, session_key, control_kind, correlation_id,
                meta_skill_name, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
        connection.commit()
        try:
            connection.execute(
                """
                INSERT INTO meta_control_intents (
                    intent_id, session_key, control_kind, correlation_id,
                    meta_skill_name, status, created_at, updated_at
                ) VALUES ('intent-2', 'agent:main:test', 'manual',
                          'request:req-1', 'other', 'staged', 2, 2)
                """
            )
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover - explicit invariant failure
            raise AssertionError("duplicate control correlation was accepted")
    finally:
        connection.close()

    backend = get_backend(f"sqlite:///{db_path}")
    migrations = read_migrations(str(MIGRATIONS_DIR)).filter(
        lambda migration: migration.id == V030_ID
    )
    with backend.lock():
        backend.rollback_migrations(migrations)

    connection = sqlite3.connect(db_path)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='meta_control_intents'"
        ).fetchone()
        assert exists is None
    finally:
        connection.close()
