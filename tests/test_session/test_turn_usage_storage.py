from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage, StorageBusyError


@pytest.mark.asyncio
async def test_transcript_entry_turn_usage_round_trips() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        node = SessionNode(session_key="agent:main:webchat:test", session_id="sid-test")
        await storage.upsert_session(node)
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id=node.session_id,
                session_key=node.session_key,
                role="assistant",
                content="done",
                turn_usage={
                    "model": "openai/gpt-test",
                    "input_tokens": 11,
                    "output_tokens": 5,
                    "cost_usd": 0.0123,
                },
            )
        )

        entries = await storage.get_transcript(node.session_id)

        assert entries[0].turn_usage == {
            "model": "openai/gpt-test",
            "input_tokens": 11,
            "output_tokens": 5,
            "cost_usd": 0.0123,
        }
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_transcript_entry_turn_context_round_trips() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        node = SessionNode(session_key="agent:main:webchat:identity", session_id="sid-identity")
        await storage.upsert_session(node)
        expected = {
            "turn_id": "turn-1",
            "client_message_id": "client-1",
            "surface_id": "tui:test",
            "intent": "send",
            "disposition": "applied",
            "revision": 1,
        }
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id=node.session_id,
                session_key=node.session_key,
                role="assistant",
                content="done",
                turn_context=expected,
            )
        )

        entries = await storage.get_transcript(node.session_id)

        assert entries[0].turn_context == expected
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_transcript_turn_context_disposition_can_be_rebound() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        node = SessionNode(session_key="agent:main:webchat:rebind", session_id="sid-rebind")
        await storage.upsert_session(node)
        entry = TranscriptEntry(
            session_id=node.session_id,
            session_key=node.session_key,
            role="user",
            content="late steer",
            turn_context={"turn_id": "old", "disposition": "steering"},
        )
        await storage.append_transcript_entry(entry)

        promoted = {
            "turn_id": "new",
            "intent": "steer",
            "disposition": "promoted",
            "target_turn_id": "old",
            "promoted_from_turn_id": "old",
            "revision": 2,
        }
        assert await storage.update_transcript_turn_context(
            node.session_key,
            entry.message_id,
            promoted,
        )

        rows = await storage.get_transcript(node.session_id)
        assert rows[0].turn_context == promoted
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_transcript_turn_context_update_honors_transaction_gate() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        node = SessionNode(session_key="agent:main:webchat:gate", session_id="sid-gate")
        await storage.upsert_session(node)
        entry = TranscriptEntry(
            session_id=node.session_id,
            session_key=node.session_key,
            role="user",
            content="queued input",
            turn_context={"turn_id": "turn-old", "disposition": "queued"},
        )
        await storage.append_transcript_entry(entry)

        storage._busy_budget_seconds = 0.0
        await storage._operation_lock.acquire()
        try:
            with pytest.raises(StorageBusyError) as caught:
                await storage.update_transcript_turn_context(
                    node.session_key,
                    entry.message_id,
                    {"turn_id": "turn-new", "disposition": "promoted"},
                )
            assert caught.value.operation == "update_transcript_turn_context"
        finally:
            storage._operation_lock.release()

        rows = await storage.get_transcript(node.session_id)
        assert rows[0].turn_context == {
            "turn_id": "turn-old",
            "disposition": "queued",
        }
    finally:
        if storage._operation_lock.locked():
            storage._operation_lock.release()
        await storage.close()


def test_v010_adds_transcript_turn_usage_column() -> None:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "V010__transcript_turn_usage.py"
    )
    spec = importlib.util.spec_from_file_location("v010_turn_usage", migration_path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    with patch("yoyo.step", lambda apply, rollback: (apply, rollback)):
        spec.loader.exec_module(migration)
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            """
            CREATE TABLE transcript_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                session_key TEXT NOT NULL,
                message_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT
            )
            """
        )

        migration.apply_step(conn)
        migration.apply_step(conn)

        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(transcript_entries)")
        }
        assert "turn_usage" in columns
    finally:
        conn.close()
