from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from opensquilla.gateway.memory_repair_service import (
    claim_repair_receipt,
    list_repair_queue,
)
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    MemoryDurableReceipt,
    SessionNode,
    TranscriptEntry,
)
from opensquilla.session.storage import SessionStorage

SESSION_KEY = "agent:main:webchat:repair-gate"
SESSION_ID = "session-repair-gate"


def _session() -> SessionNode:
    return SessionNode(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        agent_id="main",
        created_at=100,
        updated_at=100,
        epoch=0,
    )


def _entry() -> TranscriptEntry:
    return TranscriptEntry(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        message_id="message-repair-gate",
        role="user",
        content="hello",
        created_at=200,
    )


def _task() -> AgentTaskRecord:
    return AgentTaskRecord(
        task_id="task-repair-gate",
        session_key=SESSION_KEY,
        agent_id="main",
        source_kind="webui",
        queue_mode="followup",
        run_kind="web_turn",
        status=AgentTaskStatus.QUEUED,
        created_at=200,
        updated_at=200,
    )


async def _accept(storage: SessionStorage) -> Any:
    return await storage.accept_turn(
        _entry(),
        expected_epoch=0,
        updated_at=200,
        task_record=_task(),
        source_scope="webui",
        request_session_key=SESSION_KEY,
        client_request_id="request-repair-gate",
        request_fingerprint="sha256:repair-gate",
    )


@pytest.mark.asyncio
async def test_repair_reads_and_claims_wait_for_accept_rollback(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repair tick must not observe or commit another coroutine's transaction."""

    storage = await SessionStorage.open(tmp_path / "sessions.db")
    transaction_open = asyncio.Event()
    release_accept = asyncio.Event()
    try:
        await storage.upsert_session(_session())
        claimable = await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
                scope="repair",
                source_path="memory/.raw_fallbacks/claimable.md",
                idempotency_key="repair:claimable",
                status="repair_pending",
                created_at=10,
            )
        )
        original_insert_task = storage._insert_agent_task

        async def _pause_then_fail(conn: Any, task: AgentTaskRecord) -> None:
            await original_insert_task(conn, task)
            phantom = MemoryDurableReceipt(
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
                scope="repair",
                source_path="memory/.raw_fallbacks/uncommitted.md",
                idempotency_key="repair:uncommitted",
                status="repair_pending",
                created_at=20,
                updated_at=20,
            )
            values = phantom.model_dump()
            columns = list(values)
            await conn.execute(
                f"INSERT INTO memory_durable_receipts ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [values[column] for column in columns],
            )
            transaction_open.set()
            await release_accept.wait()
            raise RuntimeError("injected accept failure")

        monkeypatch.setattr(storage, "_insert_agent_task", _pause_then_fail)
        accept_task = asyncio.create_task(_accept(storage))
        await asyncio.wait_for(transaction_open.wait(), timeout=1)

        list_task = asyncio.create_task(list_repair_queue(storage, limit=10))
        claim_task = asyncio.create_task(claim_repair_receipt(storage, claimable))
        await asyncio.sleep(0.01)

        assert not list_task.done()
        assert not claim_task.done()

        release_accept.set()
        with pytest.raises(RuntimeError, match="injected accept failure"):
            await accept_task
        listed, claimed = await asyncio.gather(list_task, claim_task)

        assert all(row.source_path != "memory/.raw_fallbacks/uncommitted.md" for row in listed)
        assert claimed is not None
        assert claimed.status == "repair_running"
        assert await storage.get_transcript(SESSION_ID) == []
        assert await storage.get_agent_task("task-repair-gate") is None
        assert (
            await storage.get_turn_ingress_receipt(
                source_scope="webui",
                request_session_key=SESSION_KEY,
                client_request_id="request-repair-gate",
            )
            is None
        )
        session = await storage.get_session(SESSION_KEY)
        assert session is not None
        assert session.updated_at == 100
        assert not await storage.memory_durable_receipt_exists_for_path(
            "memory/.raw_fallbacks/uncommitted.md"
        )
        assert storage.conn.in_transaction is False
    finally:
        release_accept.set()
        await storage.close()


@pytest.mark.asyncio
async def test_repair_claim_exception_rolls_back_without_side_effects(tmp_path) -> None:
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        receipt = await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
                scope="repair",
                source_path="memory/.raw_fallbacks/failing-claim.md",
                idempotency_key="repair:failing-claim",
                status="repair_pending",
                created_at=10,
            )
        )
        await storage.conn.execute(
            """
            CREATE TRIGGER fail_repair_claim
            BEFORE UPDATE OF status ON memory_durable_receipts
            WHEN NEW.status = 'repair_running'
            BEGIN
                SELECT RAISE(ABORT, 'injected repair claim failure');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="injected repair claim failure"):
            await claim_repair_receipt(storage, receipt)

        rows = await storage.list_memory_durable_receipts(
            idempotency_key="repair:failing-claim"
        )
        assert len(rows) == 1
        assert rows[0].status == "repair_pending"
        assert storage.conn.in_transaction is False
    finally:
        await storage.close()
