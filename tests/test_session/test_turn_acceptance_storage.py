"""Storage contract tests for durable, idempotent turn acceptance."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionNode,
    TranscriptEntry,
)
from opensquilla.session.storage import SessionStorage, StorageBusyError

SESSION_KEY = "agent:main:webchat:durable-acceptance"
SESSION_ID = "session-durable-acceptance"


def _session(*, updated_at: int = 100) -> SessionNode:
    return SessionNode(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        agent_id="main",
        created_at=100,
        updated_at=updated_at,
        epoch=0,
    )


def _entry(message_id: str, *, content: str = "hello", created_at: int = 200) -> TranscriptEntry:
    return TranscriptEntry(
        session_id=SESSION_ID,
        session_key=SESSION_KEY,
        message_id=message_id,
        role="user",
        content=content,
        created_at=created_at,
    )


def _task(task_id: str, *, updated_at: int = 200) -> AgentTaskRecord:
    return AgentTaskRecord(
        task_id=task_id,
        session_key=SESSION_KEY,
        agent_id="main",
        source_kind="webui",
        queue_mode="followup",
        run_kind="web_turn",
        status=AgentTaskStatus.QUEUED,
        created_at=updated_at,
        updated_at=updated_at,
    )


async def _accept_turn(
    storage: SessionStorage,
    *,
    message_id: str,
    task_id: str,
    request_id: str = "request-one",
    fingerprint: str = "sha256:request-one",
    updated_at: int = 200,
) -> Any:
    return await storage.accept_turn(
        _entry(message_id, created_at=updated_at),
        expected_epoch=0,
        updated_at=updated_at,
        task_record=_task(task_id, updated_at=updated_at),
        source_scope="webui",
        request_session_key=SESSION_KEY,
        client_request_id=request_id,
        request_fingerprint=fingerprint,
    )


def _result_value(result: Any, name: str) -> Any:
    """Read an accepted identifier from either a result or its receipt member."""

    if isinstance(result, dict):
        candidate = result.get("receipt", result)
    else:
        candidate = getattr(result, "receipt", result)
    if isinstance(candidate, dict):
        return candidate[name]
    return getattr(candidate, name)


async def _row_count(storage: SessionStorage, table: str) -> int:
    async with storage.conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _receipt_rows(storage: SessionStorage) -> list[dict[str, Any]]:
    async with storage.conn.execute(
        "SELECT * FROM turn_ingress_receipts ORDER BY accepted_at, receipt_id"
    ) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


@pytest.mark.asyncio
async def test_accept_turn_commits_message_session_task_and_receipt_together(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())

        result = await _accept_turn(
            storage,
            message_id="message-one",
            task_id="task-one",
        )

        transcript = await storage.get_transcript(SESSION_ID)
        session = await storage.get_session(SESSION_KEY)
        task = await storage.get_agent_task("task-one")
        receipts = await _receipt_rows(storage)

        assert [entry.message_id for entry in transcript] == ["message-one"]
        assert session is not None
        assert session.updated_at == 200
        assert task is not None
        assert task.status == AgentTaskStatus.QUEUED
        assert task.details is not None
        assert task.details["persisted_user_message_id"] == "message-one"
        assert task.details["persisted_user_message_ids"] == ["message-one"]
        assert task.details["message_count"] == 1
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt["receipt_id"]
        assert receipt["accepted_at"] >= 200
        assert {
            key: receipt[key]
            for key in (
                "source_scope",
                "request_session_key",
                "client_request_id",
                "request_fingerprint",
                "accepted_session_key",
                "session_id",
                "message_id",
                "task_id",
                "schema_version",
            )
        } == {
            "source_scope": "webui",
            "request_session_key": SESSION_KEY,
            "client_request_id": "request-one",
            "request_fingerprint": "sha256:request-one",
            "accepted_session_key": SESSION_KEY,
            "session_id": SESSION_ID,
            "message_id": "message-one",
            "task_id": "task-one",
            "schema_version": 1,
        }
        assert _result_value(result, "message_id") == "message-one"
        assert _result_value(result, "task_id") == "task-one"
        assert _result_value(result, "session_id") == SESSION_ID
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_table", ["agent_tasks", "turn_ingress_receipts"])
async def test_accept_turn_rolls_back_every_write_when_an_insert_fails(
    tmp_path,
    failing_table: str,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / f"{failing_table}.db"))
    try:
        await storage.upsert_session(_session())
        trigger_name = f"fail_acceptance_insert_{failing_table}"
        await storage.conn.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE INSERT ON {failing_table}
            BEGIN
                SELECT RAISE(ABORT, 'injected acceptance failure');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="injected acceptance failure"):
            await _accept_turn(
                storage,
                message_id="message-failed",
                task_id="task-failed",
            )

        session = await storage.get_session(SESSION_KEY)
        assert session is not None
        assert session.updated_at == 100
        assert await storage.count_transcript_entries(SESSION_ID) == 0
        assert await storage.get_agent_task("task-failed") is None
        assert await _row_count(storage, "turn_ingress_receipts") == 0
        assert storage.conn.in_transaction is False
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_replays_same_request_without_duplicate_side_effects(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        first = await _accept_turn(
            storage,
            message_id="message-original",
            task_id="task-original",
        )

        replay = await _accept_turn(
            storage,
            message_id="message-prospective-retry",
            task_id="task-prospective-retry",
            updated_at=300,
        )

        session = await storage.get_session(SESSION_KEY)
        assert session is not None
        assert session.updated_at == 200
        assert await storage.count_transcript_entries(SESSION_ID) == 1
        assert await _row_count(storage, "agent_tasks") == 1
        assert await _row_count(storage, "turn_ingress_receipts") == 1
        assert await storage.get_agent_task("task-prospective-retry") is None
        assert _result_value(replay, "receipt_id") == _result_value(first, "receipt_id")
        assert _result_value(replay, "message_id") == "message-original"
        assert _result_value(replay, "task_id") == "task-original"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_collects_into_existing_task_in_the_same_transaction(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        existing = _task("task-collect")
        existing.queue_mode = "collect"
        existing.details = {
            "message_count": 1,
            "persisted_user_message_id": "message-first",
            "persisted_user_message_ids": ["message-first"],
            "fresh_user_session": True,
            "existing_only": "preserved",
        }
        await storage.create_agent_task(existing)

        collected = _task("task-collect", updated_at=300)
        collected.queue_mode = "collect"
        collected.details = {
            "collected": True,
            "message_count": 2,
            "persisted_user_message_id": "message-first",
            "persisted_user_message_ids": [
                "message-first",
                "message-collected",
            ],
        }
        result = await storage.accept_turn(
            _entry("message-collected", content="second", created_at=300),
            expected_epoch=0,
            updated_at=300,
            task_record=collected,
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id="request-collect",
            request_fingerprint="sha256:request-collect",
            merge_into_task=True,
        )

        task = await storage.get_agent_task("task-collect")
        assert task is not None
        assert task.details is not None
        assert task.details["collected"] is True
        assert task.details["message_count"] == 2
        assert task.details["persisted_user_message_id"] == "message-first"
        assert task.details["persisted_user_message_ids"] == [
            "message-first",
            "message-collected",
        ]
        assert task.details["fresh_user_session"] is True
        assert task.details["existing_only"] == "preserved"
        assert [
            entry.message_id for entry in await storage.get_transcript(SESSION_ID)
        ] == ["message-collected"]
        assert await _row_count(storage, "agent_tasks") == 1
        assert await _row_count(storage, "turn_ingress_receipts") == 1
        assert _result_value(result, "task_id") == "task-collect"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_failed_collected_acceptance_rolls_back_task_details_and_message(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        existing = _task("task-collect")
        existing.queue_mode = "collect"
        original_details = {
            "message_count": 1,
            "persisted_user_message_id": "message-first",
            "persisted_user_message_ids": ["message-first"],
        }
        existing.details = original_details
        await storage.create_agent_task(existing)
        await storage.conn.execute(
            """
            CREATE TRIGGER fail_collected_receipt
            BEFORE INSERT ON turn_ingress_receipts
            BEGIN
                SELECT RAISE(ABORT, 'injected collected receipt failure');
            END
            """
        )
        collected = _task("task-collect", updated_at=300)
        collected.queue_mode = "collect"
        collected.details = {"collected": True, "message_count": 2}

        with pytest.raises(sqlite3.IntegrityError, match="collected receipt failure"):
            await storage.accept_turn(
                _entry("message-collected", content="second", created_at=300),
                expected_epoch=0,
                updated_at=300,
                task_record=collected,
                source_scope="webui",
                request_session_key=SESSION_KEY,
                client_request_id="request-collect",
                request_fingerprint="sha256:request-collect",
                merge_into_task=True,
            )

        task = await storage.get_agent_task("task-collect")
        assert task is not None
        assert task.details == original_details
        assert await storage.get_transcript(SESSION_ID) == []
        assert await _row_count(storage, "turn_ingress_receipts") == 0
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_rejects_request_id_reuse_with_a_different_fingerprint(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        await _accept_turn(
            storage,
            message_id="message-original",
            task_id="task-original",
        )

        with pytest.raises(Exception) as caught:
            await _accept_turn(
                storage,
                message_id="message-conflict",
                task_id="task-conflict",
                fingerprint="sha256:different-payload",
                updated_at=300,
            )

        assert caught.value.__class__.__name__ == "TurnIngressConflictError"
        session = await storage.get_session(SESSION_KEY)
        assert session is not None
        assert session.updated_at == 200
        assert await storage.count_transcript_entries(SESSION_ID) == 1
        assert await _row_count(storage, "agent_tasks") == 1
        assert await _row_count(storage, "turn_ingress_receipts") == 1
        assert await storage.get_agent_task("task-conflict") is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_busy_timeout_has_no_partial_side_effects(tmp_path) -> None:
    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    locker: sqlite3.Connection | None = None
    try:
        await storage.upsert_session(_session())
        storage._busy_budget_seconds = 0.0
        locker = sqlite3.connect(str(db_path), timeout=0.1, isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")

        with pytest.raises(StorageBusyError):
            await _accept_turn(
                storage,
                message_id="message-busy",
                task_id="task-busy",
            )

        locker.execute("ROLLBACK")
        locker.close()
        locker = None

        session = await storage.get_session(SESSION_KEY)
        assert session is not None
        assert session.updated_at == 100
        assert await storage.count_transcript_entries(SESSION_ID) == 0
        assert await storage.get_agent_task("task-busy") is None
        assert await _row_count(storage, "turn_ingress_receipts") == 0
        assert storage.conn.in_transaction is False
    finally:
        if locker is not None:
            locker.execute("ROLLBACK")
            locker.close()
        await storage.close()
