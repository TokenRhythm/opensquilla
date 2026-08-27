"""Regression tests for ``SessionStorage`` transaction isolation.

These tests exercise observable storage behaviour while using a connection
proxy only to hold a transaction at its commit boundary.  The proxy makes the
otherwise small concurrency windows deterministic without depending on wall
clock timing or external services.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Coroutine
from typing import Any

import pytest

from opensquilla.session import storage as storage_module
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionNode,
    TranscriptEntry,
)
from opensquilla.session.storage import (
    SessionStorage,
    StorageBusyError,
    bounded_interactive_storage_reads,
)

_TRANSCRIPT_SESSION_KEY = "agent:main:webchat:transcript-reader"
_TRANSCRIPT_SESSION_ID = "session-transcript-reader"


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


def _transcript_session(
    *,
    session_key: str = _TRANSCRIPT_SESSION_KEY,
    session_id: str = _TRANSCRIPT_SESSION_ID,
) -> SessionNode:
    return SessionNode(
        session_key=session_key,
        session_id=session_id,
        agent_id="main",
        created_at=100,
        updated_at=100,
        epoch=0,
    )


def _transcript_entry(
    message_id: str,
    *,
    created_at: int,
    session_key: str = _TRANSCRIPT_SESSION_KEY,
    session_id: str = _TRANSCRIPT_SESSION_ID,
) -> TranscriptEntry:
    return TranscriptEntry(
        session_id=session_id,
        session_key=session_key,
        message_id=message_id,
        role="user",
        content=f"content-{message_id}",
        created_at=created_at,
    )


async def _accept_transcript_turn(
    storage: SessionStorage,
    message_id: str,
    *,
    updated_at: int,
    session_key: str = _TRANSCRIPT_SESSION_KEY,
    session_id: str = _TRANSCRIPT_SESSION_ID,
) -> Any:
    return await storage.accept_turn(
        _transcript_entry(
            message_id,
            created_at=updated_at,
            session_key=session_key,
            session_id=session_id,
        ),
        expected_epoch=0,
        updated_at=updated_at,
        task_record=None,
        source_scope="webui",
        request_session_key=session_key,
        client_request_id=f"request-{message_id}",
        request_fingerprint=f"sha256:{message_id}",
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


class _FetchallGateCursor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def fetchall(self) -> list[Any]:
        self.started.set()
        await self.release.wait()
        return []


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
async def test_read_operation_gate_wait_is_bounded_by_the_busy_budget(tmp_path) -> None:
    """An explicitly interactive read returns busy instead of waiting forever."""

    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    storage._busy_budget_seconds = 0.05
    await storage._operation_lock.acquire()
    with bounded_interactive_storage_reads():
        read = asyncio.create_task(
            storage.get_session("agent:main:webchat:bounded-read")
        )
    try:
        with pytest.raises(StorageBusyError) as caught:
            await asyncio.wait_for(read, timeout=0.5)

        assert caught.value.operation == "get_session"
        assert caught.value.waited_ms >= 0
        assert caught.value.retry_after_ms == 100
        assert caught.value.stage == "lock_acquire"
        assert caught.value.resource == "session_storage_operation_lock"
        storage._operation_lock.release()
        assert await storage.get_session("agent:main:webchat:bounded-read") is None
    finally:
        if storage._operation_lock.locked():
            storage._operation_lock.release()
        await asyncio.gather(read, return_exceptions=True)
        await storage.close()


@pytest.mark.asyncio
async def test_internal_read_operation_gate_keeps_waiting_without_interactive_scope(
    tmp_path,
) -> None:
    """Internal and CLI reads retain the pre-existing wait-for-writer contract."""

    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    storage._busy_budget_seconds = 0.01
    await storage._operation_lock.acquire()
    read = asyncio.create_task(storage.get_session("agent:main:webchat:internal-read"))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(read), timeout=0.05)
        assert read.done() is False

        storage._operation_lock.release()
        assert await asyncio.wait_for(read, timeout=0.5) is None
    finally:
        if storage._operation_lock.locked():
            storage._operation_lock.release()
        await asyncio.gather(read, return_exceptions=True)
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


@pytest.mark.asyncio
async def test_file_wal_opens_persistent_query_only_transcript_reader(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        reader = storage._transcript_reader
        assert reader is not None
        assert reader is not storage.conn
        async with reader.execute("PRAGMA query_only") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert int(row[0]) == 1
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_bounded_transcript_reader_lock_wait_reports_storage_busy(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    storage._busy_budget_seconds = 0.0
    await storage._transcript_reader_lock.acquire()
    try:
        with bounded_interactive_storage_reads():
            with pytest.raises(StorageBusyError) as caught:
                await storage.get_transcript(_TRANSCRIPT_SESSION_ID)
        assert caught.value.operation == "get_transcript"
        assert caught.value.stage == "lock_acquire"
        assert caught.value.resource == "session_storage_transcript_reader_lock"
    finally:
        storage._transcript_reader_lock.release()
        await storage.close()


@pytest.mark.asyncio
async def test_cancelled_transcript_fetch_settles_before_releasing_connection() -> None:
    storage = SessionStorage(":memory:")
    cursor = _FetchallGateCursor()
    fetch = asyncio.create_task(storage._fetchall_transcript_rows(cursor))
    await asyncio.wait_for(cursor.started.wait(), timeout=1.0)

    fetch.cancel()
    await asyncio.sleep(0)
    assert fetch.done() is False

    cursor.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.gather(fetch)


@pytest.mark.asyncio
async def test_cancelled_reader_query_settles_before_lock_release(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    read: asyncio.Task[list[TranscriptEntry]] | None = None
    release = threading.Event()
    try:
        await storage.upsert_session(_transcript_session())
        await storage.append_transcript_entry(
            _transcript_entry("cancelled-reader", created_at=150),
            expected_epoch=0,
        )
        reader = storage._transcript_reader
        assert reader is not None
        set_progress_handler = getattr(reader, "set_progress_handler", None)
        if not callable(set_progress_handler):
            pytest.skip("active aiosqlite backend does not expose a progress handler")

        entered = threading.Event()
        calls = 0

        def block_reader_worker() -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                release.wait(5)
            return 0

        await set_progress_handler(block_reader_worker, 1)
        read = asyncio.create_task(storage.get_transcript(_TRANSCRIPT_SESSION_ID))
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=1.0)

        read.cancel()
        await asyncio.sleep(0)

        assert read.done() is False
        assert storage._transcript_reader_lock.locked() is True

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.gather(read)
        assert storage._transcript_reader_lock.locked() is False
        await set_progress_handler(None, 0)
    finally:
        release.set()
        if read is not None:
            await asyncio.gather(read, return_exceptions=True)
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_session", [False, True])
async def test_slow_transcript_reader_does_not_block_accept_turn(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    cross_session: bool,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    read: asyncio.Task[list[TranscriptEntry]] | None = None
    try:
        assert storage._transcript_reader is not None
        await storage.upsert_session(_transcript_session())
        write_session_key = (
            "agent:main:webchat:transcript-writer"
            if cross_session
            else _TRANSCRIPT_SESSION_KEY
        )
        write_session_id = (
            "session-transcript-writer" if cross_session else _TRANSCRIPT_SESSION_ID
        )
        if cross_session:
            await storage.upsert_session(
                _transcript_session(
                    session_key=write_session_key,
                    session_id=write_session_id,
                )
            )
        await storage.append_transcript_entry(
            _transcript_entry("existing", created_at=150),
            expected_epoch=0,
        )
        fetch_started = asyncio.Event()
        original_fetchall = storage._fetchall_transcript_rows

        async def slow_fetchall(cursor: Any) -> list[Any]:
            fetch_started.set()
            await asyncio.sleep(2.35)
            return await original_fetchall(cursor)

        monkeypatch.setattr(storage, "_fetchall_transcript_rows", slow_fetchall)
        read = asyncio.create_task(storage.get_transcript(_TRANSCRIPT_SESSION_ID))
        await asyncio.wait_for(fetch_started.wait(), timeout=1.0)

        await asyncio.wait_for(
            _accept_transcript_turn(
                storage,
                "accepted-during-read",
                updated_at=200,
                session_key=write_session_key,
                session_id=write_session_id,
            ),
            timeout=1.5,
        )
        assert read.done() is False

        await asyncio.wait_for(read, timeout=3.0)
        monkeypatch.setattr(storage, "_fetchall_transcript_rows", original_fetchall)
        persisted = await storage.get_transcript(write_session_id)
        expected = ["accepted-during-read"] if cross_session else [
            "existing",
            "accepted-during-read",
        ]
        assert [entry.message_id for entry in persisted] == expected
    finally:
        if read is not None:
            await asyncio.gather(read, return_exceptions=True)
        await storage.close()


@pytest.mark.asyncio
async def test_transcript_reader_observes_only_committed_writer_state(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    gate: _CommitGateConnection | None = None
    write: asyncio.Task[Any] | None = None
    try:
        assert storage._transcript_reader is not None
        await storage.upsert_session(_transcript_session())
        gate = _CommitGateConnection(storage.conn, commit_count=1)
        storage._conn = gate
        write = asyncio.create_task(
            _accept_transcript_turn(storage, "pending-commit", updated_at=200)
        )
        await gate.wait_until_commit(0)

        assert await asyncio.wait_for(
            storage.get_transcript(_TRANSCRIPT_SESSION_ID),
            timeout=0.5,
        ) == []

        gate.release_commit(0)
        await asyncio.wait_for(write, timeout=1.0)
        committed = await storage.get_transcript(_TRANSCRIPT_SESSION_ID)
        assert [entry.message_id for entry in committed] == ["pending-commit"]
    finally:
        if gate is not None:
            gate.release_all()
        if write is not None:
            await asyncio.gather(write, return_exceptions=True)
        await storage.close()


@pytest.mark.asyncio
async def test_transcript_decode_runs_after_fetch_on_worker_thread(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_transcript_session())
        await storage.append_transcript_entry(
            _transcript_entry("decode", created_at=150),
            expected_epoch=0,
        )
        event_loop_thread = threading.get_ident()
        decode_threads: list[int] = []
        original_decode = storage_module._decode_transcript_rows

        def observe_decode(rows: list[Any]) -> list[TranscriptEntry]:
            decode_threads.append(threading.get_ident())
            assert rows
            assert not isinstance(rows[0], dict)
            return original_decode(rows)

        monkeypatch.setattr(storage_module, "_decode_transcript_rows", observe_decode)

        transcript = await storage.get_transcript(_TRANSCRIPT_SESSION_ID)

        assert [entry.message_id for entry in transcript] == ["decode"]
        assert decode_threads
        assert decode_threads[0] != event_loop_thread
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_memory_transcript_reader_warns_once_and_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=storage_module.__name__)
    storage = await SessionStorage.open(":memory:")
    try:
        assert storage._transcript_reader is None
        await storage.upsert_session(_transcript_session())
        await storage.append_transcript_entry(
            _transcript_entry("memory", created_at=150),
            expected_epoch=0,
        )
        assert [
            entry.message_id
            for entry in await storage.get_transcript(_TRANSCRIPT_SESSION_ID)
        ] == ["memory"]
        storage._busy_budget_seconds = 0.0
        await storage._operation_lock.acquire()
        try:
            with bounded_interactive_storage_reads():
                with pytest.raises(StorageBusyError) as caught:
                    await storage.get_transcript(_TRANSCRIPT_SESSION_ID)
            assert caught.value.operation == "get_transcript"
            assert caught.value.resource == "session_storage_operation_lock"
        finally:
            storage._operation_lock.release()
    finally:
        await storage.close()

    warnings = [
        record
        for record in caplog.records
        if "session_storage.transcript_reader_fallback" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == (
        "session_storage.transcript_reader_fallback "
        "reason=memory_database journal_mode=memory"
    )
    assert warnings[0].reason == "memory_database"
    assert warnings[0].journal_mode == "memory"


@pytest.mark.asyncio
async def test_non_wal_transcript_reader_warns_once_without_database_path(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_path = tmp_path / "private-customer-session.db"
    storage = SessionStorage(str(private_path))
    caplog.set_level(logging.WARNING, logger=storage_module.__name__)

    await storage._open_transcript_reader("delete")
    await storage._open_transcript_reader("truncate")

    assert storage._transcript_reader is None
    warnings = [
        record
        for record in caplog.records
        if "session_storage.transcript_reader_fallback" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert warnings[0].reason == "journal_mode_not_wal"
    assert warnings[0].journal_mode == "delete"
    assert str(private_path) not in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_transcript_reader_open_failure_warns_once_and_uses_writer_fallback(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_path = tmp_path / "private-open-failure.db"
    real_connect = storage_module.aiosqlite.connect
    connect_calls = 0

    def fail_reader_connect(*args: Any, **kwargs: Any) -> Any:
        nonlocal connect_calls
        connect_calls += 1
        if connect_calls == 2:
            raise OSError("sensitive reader failure body")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(storage_module.aiosqlite, "connect", fail_reader_connect)
    caplog.set_level(logging.WARNING, logger=storage_module.__name__)
    storage = await SessionStorage.open(str(private_path))
    try:
        assert storage._transcript_reader is None
        await storage.upsert_session(_transcript_session())
        await storage.append_transcript_entry(
            _transcript_entry("fallback", created_at=150),
            expected_epoch=0,
        )
        transcript = await storage.get_transcript(_TRANSCRIPT_SESSION_ID)
        assert [entry.message_id for entry in transcript] == ["fallback"]
    finally:
        await storage.close()

    warnings = [
        record
        for record in caplog.records
        if "session_storage.transcript_reader_fallback" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert warnings[0].reason == "open_failed"
    assert warnings[0].journal_mode == "wal"
    assert str(private_path) not in warnings[0].getMessage()
    assert "sensitive reader failure body" not in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_non_ascii_database_path_releases_reader_handle_on_close(tmp_path) -> None:
    db_path = tmp_path / "会话 数据.db"
    moved_path = tmp_path / "已关闭 数据.db"
    storage = await SessionStorage.open(str(db_path))
    assert storage._transcript_reader is not None

    await storage.close()
    db_path.replace(moved_path)
    moved_path.unlink()

    assert not moved_path.exists()


@pytest.mark.asyncio
async def test_reconnect_retires_existing_connections_before_reopening(tmp_path) -> None:
    db_path = tmp_path / "reconnect 会话.db"
    moved_path = tmp_path / "reconnect 已关闭.db"
    storage = await SessionStorage.open(str(db_path))
    first_writer = storage.conn
    first_reader = storage._transcript_reader
    assert first_reader is not None

    await storage.connect()

    assert storage.conn is not first_writer
    assert storage._transcript_reader is not None
    assert storage._transcript_reader is not first_reader
    with pytest.raises((ValueError, sqlite3.ProgrammingError)):
        await first_writer.execute("SELECT 1")
    with pytest.raises((ValueError, sqlite3.ProgrammingError)):
        await first_reader.execute("SELECT 1")

    await storage.close()
    db_path.replace(moved_path)
    moved_path.unlink()
    assert not moved_path.exists()


@pytest.mark.asyncio
async def test_reconnect_after_poison_restores_reader_and_writer(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "poison-reconnect.db"))
    await storage._retire_poisoned_connection()
    assert storage._poisoned is True

    await storage.connect()
    try:
        assert storage._poisoned is False
        assert storage._transcript_reader is not None
        await storage.upsert_session(_transcript_session())
        await storage.append_transcript_entry(
            _transcript_entry("after-reconnect", created_at=150),
            expected_epoch=0,
        )
        transcript = await storage.get_transcript(_TRANSCRIPT_SESSION_ID)
        assert [entry.message_id for entry in transcript] == ["after-reconnect"]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_existing_database_reopens_with_reader_and_passes_quick_check(
    tmp_path,
) -> None:
    db_path = tmp_path / "existing-v21.db"
    storage = await SessionStorage.open(str(db_path))
    await storage.upsert_session(_transcript_session())
    await storage.append_transcript_entry(
        _transcript_entry("existing-v21", created_at=150),
        expected_epoch=0,
    )
    await storage.close()

    reopened = await SessionStorage.open(str(db_path))
    try:
        assert reopened._transcript_reader is not None
        transcript = await reopened.get_transcript(_TRANSCRIPT_SESSION_ID)
        assert [entry.message_id for entry in transcript] == ["existing-v21"]
        async with reopened.conn.execute("PRAGMA quick_check") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert str(row[0]).lower() == "ok"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_close_takes_writer_lock_before_transcript_reader_lock(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    close: asyncio.Task[None] | None = None
    await storage._transcript_reader_lock.acquire()
    try:
        close = asyncio.create_task(storage.close())
        for _ in range(100):
            if storage._operation_lock.locked():
                break
            await asyncio.sleep(0)
        assert storage._operation_lock.locked()
        assert close.done() is False
    finally:
        storage._transcript_reader_lock.release()
    assert close is not None
    await asyncio.wait_for(close, timeout=1.0)
    assert storage._transcript_reader is None
    assert storage._conn is None


@pytest.mark.asyncio
async def test_poison_retirement_closes_transcript_reader_without_lock_inversion(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    retirement: asyncio.Task[None] | None = None
    await storage._operation_lock.acquire()
    await storage._transcript_reader_lock.acquire()
    try:
        retirement = asyncio.create_task(storage._retire_poisoned_connection())
        await asyncio.sleep(0)
        assert storage._poisoned is True
        assert retirement.done() is False
    finally:
        storage._transcript_reader_lock.release()
    assert retirement is not None
    try:
        await asyncio.wait_for(retirement, timeout=1.0)
    finally:
        storage._operation_lock.release()
    assert storage._transcript_reader is None
    assert storage._conn is None
    await storage.close()
