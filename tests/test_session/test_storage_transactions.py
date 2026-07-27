"""Regression tests for ``SessionStorage`` transaction isolation.

These tests exercise observable storage behaviour while using a connection
proxy only to hold a transaction at its commit boundary.  The proxy makes the
otherwise small concurrency windows deterministic without depending on wall
clock timing or external services.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Coroutine
from typing import Any

import pytest

from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus
from opensquilla.session.storage import SessionStorage, StorageBusyError


def _agent_task(task_id: str) -> AgentTaskRecord:
    return AgentTaskRecord(
        task_id=task_id,
        session_key="agent:main:webchat:transaction-contract",
        source_kind="webui",
        queue_mode="followup",
        run_kind="web_turn",
        status=AgentTaskStatus.QUEUED,
        created_at=100,
        updated_at=100,
    )


class _AwaitableCursorContext:
    """Make an intercepted connection operation awaitable and context-manageable."""

    def __init__(self, operation: Coroutine[Any, Any, Any]) -> None:
        self._operation = operation
        self._cursor: Any | None = None

    def __await__(self):  # type: ignore[no-untyped-def]
        return self._operation.__await__()

    async def __aenter__(self) -> Any:
        self._cursor = await self._operation
        return self._cursor

    async def __aexit__(self, *_: object) -> None:
        if self._cursor is not None:
            await self._cursor.close()


class _CommitGateConnection:
    """Delegate a connection while pausing successive commit operations."""

    def __init__(self, delegate: Any, commit_count: int) -> None:
        self._delegate = delegate
        self._entered = [asyncio.Event() for _ in range(commit_count)]
        self._release = [asyncio.Event() for _ in range(commit_count)]
        self._commit_index = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def _before_commit(self) -> None:
        index = self._commit_index
        self._commit_index += 1
        if index >= len(self._entered):
            return
        self._entered[index].set()
        await self._release[index].wait()

    async def commit(self) -> None:
        await self._before_commit()
        await self._delegate.commit()

    def execute(self, sql: str, params: Any = ()) -> Any:
        if sql.strip().rstrip(";").upper() != "COMMIT":
            return self._delegate.execute(sql, params)

        async def _commit_sql() -> Any:
            await self._before_commit()
            return await self._delegate.execute(sql, params)

        return _AwaitableCursorContext(_commit_sql())

    async def wait_until_commit(self, index: int) -> None:
        await asyncio.wait_for(self._entered[index].wait(), timeout=1.0)

    def release_commit(self, index: int) -> None:
        self._release[index].set()

    def release_all(self) -> None:
        for event in self._release:
            event.set()


class _RollbackGateConnection:
    """Delegate a connection while pausing rollback after it starts."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self._entered = asyncio.Event()
        self._release = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def rollback(self) -> None:
        self._entered.set()
        await self._release.wait()
        await self._delegate.rollback()

    async def wait_until_rollback(self) -> None:
        await asyncio.wait_for(self._entered.wait(), timeout=1.0)

    def release_rollback(self) -> None:
        self._release.set()


@pytest.mark.asyncio
async def test_integrity_error_rolls_back_before_an_external_writer_runs(tmp_path) -> None:
    """A failed write must not leave the shared connection holding a DB lock."""

    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    try:
        task = _agent_task("duplicate-task")
        await storage.create_agent_task(task)

        with pytest.raises(sqlite3.IntegrityError):
            await storage.create_agent_task(task)

        external = sqlite3.connect(str(db_path), timeout=0.05, isolation_level=None)
        try:
            external.execute("BEGIN IMMEDIATE")
            external.execute("ROLLBACK")
        finally:
            external.close()

        assert storage.conn.in_transaction is False
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_concurrent_task_creates_do_not_share_a_transaction(tmp_path) -> None:
    """A second write may not reach commit before the first write commits."""

    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    gate = _CommitGateConnection(storage.conn, commit_count=2)
    storage._conn = gate
    writes = [
        asyncio.create_task(storage.create_agent_task(_agent_task("task-one"))),
        asyncio.create_task(storage.create_agent_task(_agent_task("task-two"))),
    ]
    try:
        await gate.wait_until_commit(0)

        # If both public operations share the connection's implicit transaction,
        # they will both reach commit while the first commit is still paused.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(gate._entered[1].wait(), timeout=0.1)

        gate.release_commit(0)
        await gate.wait_until_commit(1)
        gate.release_commit(1)
        await asyncio.gather(*writes)

        assert await storage.get_agent_task("task-one") is not None
        assert await storage.get_agent_task("task-two") is not None
    finally:
        gate.release_all()
        await asyncio.gather(*writes, return_exceptions=True)
        await storage.close()


@pytest.mark.asyncio
async def test_read_waits_instead_of_observing_an_uncommitted_task(tmp_path) -> None:
    """Reads on the shared connection must not expose another operation's phantom."""

    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    gate = _CommitGateConnection(storage.conn, commit_count=1)
    storage._conn = gate
    writer = asyncio.create_task(storage.create_agent_task(_agent_task("pending-task")))
    reader: asyncio.Task[AgentTaskRecord | None] | None = None
    try:
        await gate.wait_until_commit(0)
        reader = asyncio.create_task(storage.get_agent_task("pending-task"))

        # A transaction-level operation gate keeps the read pending until the
        # write is committed.  Without it, the same connection sees its own
        # uncommitted INSERT and returns a phantom row.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(reader), timeout=0.1)

        gate.release_commit(0)
        await writer
        assert (await reader) is not None
    finally:
        gate.release_all()
        pending: list[asyncio.Task[Any]] = [writer]
        if reader is not None:
            pending.append(reader)
        await asyncio.gather(*pending, return_exceptions=True)
        await storage.close()


@pytest.mark.asyncio
async def test_operation_gate_wait_is_bounded_by_the_write_busy_budget(tmp_path) -> None:
    """Concurrent writers must not consume one full busy budget each in series."""

    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    storage._busy_budget_seconds = 0.05
    await storage._operation_lock.acquire()
    writes = [
        asyncio.create_task(storage.create_agent_task(_agent_task("gate-task-one"))),
        asyncio.create_task(storage.create_agent_task(_agent_task("gate-task-two"))),
    ]
    try:
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*writes, return_exceptions=True),
                timeout=0.5,
            )
            assert all(isinstance(result, StorageBusyError) for result in results)
            assert {
                result.operation
                for result in results
                if isinstance(result, StorageBusyError)
            } == {"create_agent_task"}
        finally:
            storage._operation_lock.release()
            await asyncio.gather(*writes, return_exceptions=True)

        await storage.create_agent_task(_agent_task("gate-recovered"))
        assert await storage.get_agent_task("gate-recovered") is not None
    finally:
        if storage._operation_lock.locked():
            storage._operation_lock.release()
        await asyncio.gather(*writes, return_exceptions=True)
        await storage.close()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_rollback_does_not_poison_connection(
    tmp_path,
) -> None:
    """A settled rollback stays successful when its caller is cancelled again."""

    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    gate = _RollbackGateConnection(storage.conn)
    storage._conn = gate
    body_entered = asyncio.Event()
    never_release_body = asyncio.Event()

    async def _cancelled_write() -> None:
        async with storage._write_transaction("cancelled_write"):
            body_entered.set()
            await never_release_body.wait()

    write = asyncio.create_task(_cancelled_write())
    try:
        await asyncio.wait_for(body_entered.wait(), timeout=1.0)
        write.cancel()
        await gate.wait_until_rollback()
        write.cancel()
        gate.release_rollback()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(write, timeout=1.0)

        assert storage._poisoned is False
        assert storage.conn.in_transaction is False
        await storage.create_agent_task(_agent_task("post-cancel-task"))
        assert await storage.get_agent_task("post-cancel-task") is not None
    finally:
        gate.release_rollback()
        await asyncio.gather(write, return_exceptions=True)
        await storage.close()
