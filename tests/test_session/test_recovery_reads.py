"""Real SQLite cancellation, bounded physical work, and read-path coverage."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from opensquilla.compat import aiosqlite
from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.recovery_reads import (
    ReadCancelToken,
    RecoveryReadPool,
    recovery_read_scope,
)
from opensquilla.session.storage import (
    SessionStorage,
    StorageBusyError,
    StorageConnectionPoisonedError,
)
from tests.helpers.sqlite_process_probe import run_sqlite_probe

_CLOSE_PROBE = Path(__file__).resolve().parents[1] / "fixtures" / "recovery_close_probe.py"


@pytest.fixture(params=[False, True], ids=["aiosqlite", "sqlite3-fallback"])
def sqlite_backend(request: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aiosqlite, "_FORCE_SQLITE3_FALLBACK", request.param)


@pytest.fixture
async def storage(tmp_path: Any, sqlite_backend: None) -> AsyncIterator[SessionStorage]:
    result = await SessionStorage.open(str(tmp_path / "recovery.db"))
    await result.upsert_session(SessionNode(
        session_key="agent:main:webchat:healthy", session_id="healthy",
        agent_id="main", created_at=100, updated_at=100,
    ))
    await result.append_transcript_entry(TranscriptEntry(
        session_key="agent:main:webchat:healthy", session_id="healthy",
        message_id="message-1", role="user", content="synthetic history", created_at=100,
    ), expected_epoch=0)
    try:
        yield result
    finally:
        await result.close()


class NativeGate:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def block(self) -> int:
        self.entered.set()
        if not self.release.wait(10):
            raise RuntimeError("Synthetic native gate was not released")
        return 1

    async def wait(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, 2), "native read did not start"


async def install_gate(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch, gate: NativeGate,
) -> None:
    original = storage._open_recovery_reader

    async def open_reader() -> Any:
        reader = await original()
        await reader.create_function("recovery_test_gate", 0, gate.block)
        return reader

    monkeypatch.setattr(storage, "_open_recovery_reader", open_reader)


async def test_cancelled_native_read_keeps_permit_and_other_session_recovers(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = NativeGate()
    await install_gate(storage, monkeypatch, gate)
    token = ReadCancelToken()
    with recovery_read_scope("agent:main:webchat:slow", deadline=time.monotonic() + 5,
                             cancel_token=token) as budget:
        read = asyncio.create_task(storage._read_history_query("SELECT recovery_test_gate()", ()))
    try:
        await gate.wait()
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(read, 0.5)
        pool = storage._recovery_read_pool
        assert pool is not None
        assert (pool.physical_count, pool.active_count, pool.retiring_count) == (1, 1, 1)
        for _ in range(100):
            with recovery_read_scope("agent:main:webchat:slow", deadline=time.monotonic() + 1):
                with pytest.raises(StorageBusyError) as caught:
                    await storage.get_session("agent:main:webchat:slow")
                assert caught.value.stage == "permit"
        assert pool.physical_count == 1
        with recovery_read_scope("agent:main:webchat:healthy", deadline=time.monotonic() + 1):
            node = await asyncio.wait_for(storage.get_session("agent:main:webchat:healthy"), 0.5)
            entries = await storage.get_transcript("healthy")
        assert node is not None and entries[0].content == "synthetic history"
        assert not gate.release.is_set()
        assert pool.active_count == 1
        # The writer also stays usable while the cancelled native reader runs.
        await asyncio.wait_for(storage.upsert_session(node), 0.5)
        drain = asyncio.create_task(budget.drain())
        await asyncio.sleep(0)
        drain.cancel()
        await asyncio.sleep(0)
        drain.cancel()
        await asyncio.sleep(0)
        assert not drain.done()
    finally:
        gate.release.set()
        await asyncio.gather(read, return_exceptions=True)
        await asyncio.wait_for(budget.drain(), 2)
        await drain
    assert pool.active_count == pool.retiring_count == 0


async def test_bulk_saturation_leaves_identity_capacity(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = NativeGate()
    entered = 0
    all_entered = threading.Event()
    original_block = gate.block

    def block() -> int:
        nonlocal entered
        entered += 1
        if entered == 3:
            all_entered.set()
        return original_block()

    monkeypatch.setattr(gate, "block", block)
    await install_gate(storage, monkeypatch, gate)
    reads = []
    budgets = []
    try:
        for index in range(3):
            with recovery_read_scope(f"agent:main:webchat:bulk-{index}",
                                     deadline=time.monotonic() + 5) as budget:
                reads.append(asyncio.create_task(storage._read_history_query(
                    "SELECT recovery_test_gate()", (),
                )))
                budgets.append(budget)
        assert await asyncio.to_thread(all_entered.wait, 2)
        with recovery_read_scope("agent:main:webchat:fourth", deadline=time.monotonic() + 1):
            with pytest.raises(StorageBusyError):
                await storage.get_session("agent:main:webchat:healthy")
        with recovery_read_scope("agent:main:webchat:healthy", deadline=time.monotonic() + 1,
                                 workload="identity"):
            assert await asyncio.wait_for(storage.get_session("agent:main:webchat:healthy"), 0.5)
        pool = storage._recovery_read_pool
        assert pool is not None and pool.physical_count == 4 and pool.active_count == 3
        assert not gate.release.is_set()
    finally:
        gate.release.set()
        await asyncio.gather(*reads, return_exceptions=True)
        for budget in budgets:
            await budget.drain()


async def test_title_reads_have_unique_identity_when_monotonic_ns_is_constant(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Windows Python 3.12 can return the same tick for distinct requests. Keep
    # the deadline clock running while making that identity collision certain.
    monkeypatch.setattr(time, "monotonic_ns", lambda: 123456789)
    release = asyncio.Event()
    admitted = asyncio.Event()
    keys = []
    remaining = []
    original_initialize = storage._initialize_recovery_reader

    async def initialize(reader: Any) -> None:
        await release.wait()
        await original_initialize(reader)

    pool = RecoveryReadPool(storage._open_recovery_reader, initialize=initialize)
    storage._recovery_read_pool = pool
    original_run = pool.run

    async def record_run(budget: Any, read: Any) -> Any:
        keys.append(budget.key)
        remaining.append(budget.remaining)
        if len(keys) == 4:
            admitted.set()
        return await original_run(budget, read)

    monkeypatch.setattr(pool, "run", record_run)
    reads = [asyncio.create_task(method(["healthy"])) for method in (
        storage.list_user_transcript_content_batch,
        storage.list_canonical_user_transcript_content_batch,
    ) for _ in range(2)]
    try:
        await asyncio.wait_for(admitted.wait(), 1)
        assert len(set(keys)) == 4
        assert pool.active_count == 4
        assert all(1.5 < seconds <= 2.0 for seconds in remaining)
    finally:
        release.set()
        results = await asyncio.gather(*reads, return_exceptions=True)
    assert results == [{"healthy": ["synthetic history"]}] * 4
    assert pool.active_count == 0


async def test_saturated_title_read_does_not_bypass_pool(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = NativeGate()
    entered = 0
    all_entered = threading.Event()
    original_block = gate.block

    def block() -> int:
        nonlocal entered
        entered += 1
        if entered == 4:
            all_entered.set()
        return original_block()

    monkeypatch.setattr(gate, "block", block)
    await install_gate(storage, monkeypatch, gate)
    reads = []
    budgets = []
    legacy_queries: list[str] = []
    await storage._transcript_reader.set_trace_callback(legacy_queries.append)
    await storage.conn.set_trace_callback(legacy_queries.append)
    try:
        for index in range(4):
            with recovery_read_scope(
                f"agent:main:webchat:identity-{index}",
                deadline=time.monotonic() + 5,
                workload="identity",
            ) as budget:
                reads.append(asyncio.create_task(storage._read_history_query(
                    "SELECT recovery_test_gate()", (),
                )))
                budgets.append(budget)
        assert await asyncio.to_thread(all_entered.wait, 2)
        with pytest.raises(StorageBusyError) as caught:
            await storage.list_user_transcript_content_batch(["healthy"])
        assert caught.value.stage == "permit"
        assert caught.value.resource == "session_storage_recovery_read_pool"
        assert storage._recovery_read_pool.active_count == 4
        assert storage._recovery_read_pool.physical_count == 4
        assert legacy_queries == []
    finally:
        gate.release.set()
        await asyncio.gather(*reads, return_exceptions=True)
        for budget in budgets:
            await budget.drain()
        await storage._transcript_reader.set_trace_callback(None)
        await storage.conn.set_trace_callback(None)


async def test_title_deadline_does_not_fall_back_after_initialization_stalls(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    budgets = []
    legacy_queries: list[str] = []

    async def initialize(reader: Any) -> None:
        await release.wait()
        await storage._initialize_recovery_reader(reader)

    pool = RecoveryReadPool(storage._open_recovery_reader, initialize=initialize)
    storage._recovery_read_pool = pool
    original_run = pool.run

    async def record_run(budget: Any, read: Any) -> Any:
        budgets.append(budget)
        return await original_run(budget, read)

    monkeypatch.setattr(pool, "run", record_run)
    await storage._transcript_reader.set_trace_callback(legacy_queries.append)
    await storage.conn.set_trace_callback(legacy_queries.append)
    try:
        with pytest.raises(StorageBusyError) as caught:
            await storage.list_user_transcript_content_batch(["healthy"])
        assert caught.value.stage == "deadline"
        assert caught.value.waited_ms >= 1900
        assert pool.active_count == 1
        assert legacy_queries == []
    finally:
        release.set()
        for budget in budgets:
            await budget.drain()
        await storage._transcript_reader.set_trace_callback(None)
        await storage.conn.set_trace_callback(None)


async def test_deadline_interrupts_sql_and_clears_handler_before_reuse(
    storage: SessionStorage,
) -> None:
    with recovery_read_scope("agent:main:webchat:slow", deadline=time.monotonic() + 0.05) as budget:
        with pytest.raises(StorageBusyError) as caught:
            await storage._read_history_query(
                "WITH RECURSIVE numbers(n) AS (VALUES(1) UNION ALL "
                "SELECT n + 1 FROM numbers WHERE n < 100000000) SELECT SUM(n) FROM numbers", (),
            )
    assert caught.value.stage == "deadline"
    await asyncio.wait_for(budget.drain(), 1)
    pool = storage._recovery_read_pool
    assert pool is not None and pool.physical_count == 1 and pool.active_count == 0
    with recovery_read_scope("agent:main:webchat:healthy", deadline=time.monotonic() + 1):
        assert await storage.get_session("agent:main:webchat:healthy") is not None
    assert pool.physical_count == 1


async def test_all_recovery_read_paths_bypass_busy_legacy_reader(storage: SessionStorage) -> None:
    gate = NativeGate()
    reader = storage._transcript_reader
    await reader.create_function("recovery_test_gate", 0, gate.block)

    async def legacy_read() -> None:
        async with reader.execute("SELECT recovery_test_gate()") as cursor:
            await cursor.fetchone()

    blocked = asyncio.create_task(legacy_read())
    try:
        await gate.wait()
        assert storage._recovery_read_pool is None
        with recovery_read_scope("agent:main:webchat:healthy", deadline=time.monotonic() + 1):
            assert await storage.get_session("agent:main:webchat:healthy")
            assert len(await storage.get_transcript("healthy")) == 1
            assert len(await storage.get_canonical_transcript("healthy")) == 1
            assert await storage.get_canonical_transcript_entry("healthy", "message-1")
            assert (await storage.get_canonical_transcript_coverage("healthy")).canonical_complete
            entries, more = await storage.get_canonical_transcript_page("healthy", limit=10)
            assert len(entries) == 1 and not more
        assert not gate.release.is_set()
    finally:
        gate.release.set()
        await blocked


async def test_memory_cancel_does_not_interrupt_existing_writer(sqlite_backend: None) -> None:
    storage = await SessionStorage.open(":memory:")
    await storage._operation_lock.acquire()
    try:
        with recovery_read_scope("agent:main:webchat:memory", deadline=time.monotonic() + 0.05
                                 ) as budget:
            with pytest.raises(StorageBusyError):
                await storage.get_session("agent:main:webchat:memory")
        await asyncio.wait_for(budget.drain(), 0.5)
        assert storage._operation_lock.locked()
        assert storage._recovery_read_pool is not None
        assert storage._recovery_read_pool.physical_count == 0
        async with storage.conn.execute("SELECT 42") as cursor:
            assert (await cursor.fetchone())[0] == 42
    finally:
        storage._operation_lock.release()
        await storage.close()


async def test_memory_native_cancel_retains_writer_gate(sqlite_backend: None) -> None:
    storage = await SessionStorage.open(":memory:")
    gate = NativeGate()
    await storage.conn.create_function("recovery_test_gate", 0, gate.block)
    with recovery_read_scope("agent:main:webchat:memory", deadline=time.monotonic() + 5) as budget:
        read = asyncio.create_task(storage._read_history_query("SELECT recovery_test_gate()", ()))
    try:
        await gate.wait()
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(read, 0.5)
        assert storage._operation_lock.locked()
        assert storage._recovery_read_pool is not None
        assert storage._recovery_read_pool.retiring_count == 1
    finally:
        gate.release.set()
        await asyncio.gather(read, return_exceptions=True)
        await asyncio.wait_for(budget.drain(), 2)
    try:
        assert not storage._operation_lock.locked()
        assert not storage.conn.in_transaction
        assert await storage.get_session("agent:main:webchat:memory") is None
    finally:
        await storage.close()


@pytest.mark.ci_serial
@pytest.mark.parametrize("close_delay_seconds", [0.0, 2.1])
def test_close_drains_native_before_reconnect(
    tmp_path: Path, sqlite_backend: None,
    close_delay_seconds: float,
) -> None:
    result = run_sqlite_probe(
        tmp_path, _CLOSE_PROBE,
        (str(int(aiosqlite._FORCE_SQLITE3_FALLBACK)), str(close_delay_seconds), "none"),
    )
    assert result.timed_out is None, result.output
    assert result.returncode == 0, result.output
    assert "recovery_close_contract=passed" in result.output


@pytest.mark.ci_serial
@pytest.mark.parametrize(("fault", "message"), [
    ("skip-drain", "connection close started before native reader drained"),
    ("no-close", "writer close returned without closing the connection"),
])
def test_close_probe_rejects_broken_resource_contracts(
    tmp_path: Path, sqlite_backend: None, fault: str, message: str,
) -> None:
    result = run_sqlite_probe(
        tmp_path, _CLOSE_PROBE,
        (str(int(aiosqlite._FORCE_SQLITE3_FALLBACK)), "0", fault),
    )
    assert result.timed_out is None, result.output
    assert result.returncode != 0, result.output
    assert message in result.output


@pytest.mark.ci_serial
def test_close_probe_watchdog_terminates_noncooperative_close(tmp_path: Path) -> None:
    result = run_sqlite_probe(
        tmp_path, _CLOSE_PROBE, ("0", "0", "hang-close"), execution_seconds=3,
    )
    assert result.timed_out == "execution/cleanup", result.output
    assert result.returncode != 0
    assert "phase=writer-close" in result.output


async def test_read_transaction_is_rolled_back_before_reader_reuse(storage: SessionStorage) -> None:
    borrowed = None

    async def begin(reader: Any) -> None:
        nonlocal borrowed
        borrowed = reader
        async with reader.execute("BEGIN"):
            pass

    with recovery_read_scope("agent:main:webchat:healthy", deadline=time.monotonic() + 1):
        await storage._run_recovery_read("synthetic_snapshot", begin)
    assert borrowed is not None and not borrowed.in_transaction
    with recovery_read_scope("agent:main:webchat:healthy", deadline=time.monotonic() + 1):
        with pytest.raises(aiosqlite.OperationalError, match="readonly"):
            await storage._read_history_query("DELETE FROM sessions", ())
    assert await storage.get_session("agent:main:webchat:healthy") is not None


class CleanupFailureReader:
    def __init__(self, reader: Any, *, fail_close: bool = False) -> None:
        self.reader = reader
        self.fail_close = fail_close

    def __getattr__(self, name: str) -> Any:
        return getattr(self.reader, name)

    @property
    def row_factory(self) -> Any:
        return self.reader.row_factory

    @row_factory.setter
    def row_factory(self, factory: Any) -> None:
        self.reader.row_factory = factory

    async def rollback(self) -> None:
        raise RuntimeError("synthetic rollback failure")

    async def close(self) -> None:
        if self.fail_close:
            self.fail_close = False
            raise RuntimeError("synthetic close failure")
        await self.reader.close()


async def test_unproven_close_retains_capacity_and_same_key_fence(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = storage._open_recovery_reader
    opened = 0

    async def open_reader() -> Any:
        nonlocal opened
        opened += 1
        reader = await original()
        return CleanupFailureReader(reader, fail_close=True) if opened == 1 else reader

    monkeypatch.setattr(storage, "_open_recovery_reader", open_reader)

    async def begin(reader: Any) -> None:
        async with reader.execute("BEGIN"):
            pass

    with recovery_read_scope("agent:main:webchat:bad-cleanup", deadline=time.monotonic() + 1):
        with pytest.raises(RuntimeError, match="synthetic close failure"):
            await storage._run_recovery_read("synthetic_snapshot", begin)
    pool = storage._recovery_read_pool
    assert pool is not None
    assert (pool.physical_count, pool.active_count, pool.retiring_count) == (1, 1, 1)
    with recovery_read_scope("agent:main:webchat:bad-cleanup", deadline=time.monotonic() + 1):
        with pytest.raises(StorageBusyError):
            await storage.get_session("agent:main:webchat:bad-cleanup")
    with recovery_read_scope("agent:main:webchat:healthy", deadline=time.monotonic() + 1):
        assert await storage.get_session("agent:main:webchat:healthy")
    assert opened == 2
    await pool.close()
    assert pool.active_count == pool.physical_count == 0


async def test_memory_rollback_failure_poisoned_before_writer_gate_released(
    sqlite_backend: None,
) -> None:
    storage = await SessionStorage.open(":memory:")
    storage._conn = CleanupFailureReader(storage.conn)

    async def begin(reader: Any) -> None:
        async with reader.execute("BEGIN"):
            pass

    try:
        with recovery_read_scope("agent:main:webchat:memory", deadline=time.monotonic() + 1):
            with pytest.raises(RuntimeError, match="synthetic rollback failure"):
                await storage._run_recovery_read("synthetic_snapshot", begin)
        assert not storage._operation_lock.locked()
        with pytest.raises(StorageConnectionPoisonedError):
            await storage.get_session("agent:main:webchat:memory")
    finally:
        await storage.close()


async def test_cancelled_decode_is_supervised_without_holding_database_lease(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.session import storage as storage_module

    gate = NativeGate()
    original = storage_module._decode_transcript_rows

    def decode(rows: Any) -> Any:
        gate.block()
        return original(rows)

    monkeypatch.setattr(storage_module, "_decode_transcript_rows", decode)
    with recovery_read_scope("agent:main:webchat:healthy", deadline=time.monotonic() + 5) as budget:
        read = asyncio.create_task(storage.get_canonical_transcript("healthy"))
    try:
        await gate.wait()
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(read, 0.5)
        pool = storage._recovery_read_pool
        assert pool is not None and pool.active_count == 0
        drain = asyncio.create_task(budget.drain())
        await asyncio.sleep(0)
        assert not drain.done()
        with recovery_read_scope("agent:main:webchat:other", deadline=time.monotonic() + 1):
            assert await storage.get_session("agent:main:webchat:healthy")
        assert not gate.release.is_set()
    finally:
        gate.release.set()
        await asyncio.gather(read, return_exceptions=True)
        await asyncio.wait_for(budget.drain(), 2)
    await drain


async def test_non_wal_file_uses_bounded_serial_fallback(storage: SessionStorage) -> None:
    await storage._transcript_reader.close()
    storage._transcript_reader = None
    async with storage.conn.execute("PRAGMA journal_mode=DELETE") as cursor:
        assert (await cursor.fetchone())[0] == "delete"
    with recovery_read_scope("agent:main:webchat:healthy", deadline=time.monotonic() + 1):
        assert len(await storage.get_canonical_transcript("healthy")) == 1
    pool = storage._recovery_read_pool
    assert pool is not None and pool.physical_count == 0 and pool.active_count == 0
    assert not storage._operation_lock.locked()


async def test_recovery_reads_observe_committed_wal_state(storage: SessionStorage) -> None:
    async with storage._write_transaction("synthetic_pending_write") as writer:
        async with writer.execute(
            "UPDATE transcript_entries SET content = ? WHERE session_id = ?",
            ("uncommitted synthetic history", "healthy"),
        ):
            pass
        with recovery_read_scope("agent:main:webchat:healthy", deadline=time.monotonic() + 1):
            entries = await storage.get_canonical_transcript("healthy")
        assert entries[0].content == "synthetic history"
    with recovery_read_scope("agent:main:webchat:healthy", deadline=time.monotonic() + 1):
        entries = await storage.get_canonical_transcript("healthy")
    assert entries[0].content == "uncommitted synthetic history"


async def test_failed_idle_close_retains_reader_until_retry(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = storage._open_recovery_reader
    reader = None

    async def open_reader() -> Any:
        nonlocal reader
        reader = CleanupFailureReader(await original(), fail_close=True)
        return reader

    monkeypatch.setattr(storage, "_open_recovery_reader", open_reader)
    with recovery_read_scope("agent:main:webchat:healthy", deadline=time.monotonic() + 1):
        assert await storage.get_session("agent:main:webchat:healthy")
    pool = storage._recovery_read_pool
    assert pool is not None
    with pytest.raises(RuntimeError, match="synthetic close failure"):
        await pool.close()
    assert pool.physical_count == 1 and pool._idle == [reader]
    await pool.close()
    assert pool.physical_count == 0 and not pool._idle


async def test_failed_initialization_closes_reader_before_replacement(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize = storage._initialize_recovery_reader
    opened: list[Any] = []
    original = storage._open_recovery_reader

    async def open_reader() -> Any:
        reader = await original()
        opened.append(reader)
        return reader

    async def initialize_once(reader: Any) -> None:
        if len(opened) == 1:
            raise RuntimeError("synthetic initialization failure")
        await initialize(reader)

    monkeypatch.setattr(storage, "_open_recovery_reader", open_reader)
    monkeypatch.setattr(storage, "_initialize_recovery_reader", initialize_once)
    key = "agent:main:webchat:healthy"
    with recovery_read_scope(key, deadline=time.monotonic() + 1):
        with pytest.raises(RuntimeError, match="synthetic initialization failure"):
            await storage.get_session(key)
    pool = storage._recovery_read_pool
    assert pool is not None and pool.physical_count == pool.active_count == 0
    assert not pool._idle
    with pytest.raises((ValueError, aiosqlite.ProgrammingError)):
        async with opened[0].execute("SELECT 1"):
            pass
    with recovery_read_scope(key, deadline=time.monotonic() + 1):
        assert await storage.get_session(key)
    assert len(opened) == 2 and pool.physical_count == 1


async def test_failed_initialization_and_close_keep_physical_cap_until_retry(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = storage._open_recovery_reader
    opened: list[CleanupFailureReader] = []

    async def open_reader() -> Any:
        reader = CleanupFailureReader(await original(), fail_close=True)
        opened.append(reader)
        return reader

    async def fail_configuration(reader: Any) -> None:
        raise RuntimeError("synthetic initialization failure")

    monkeypatch.setattr(storage, "_open_recovery_reader", open_reader)
    monkeypatch.setattr(storage, "_initialize_recovery_reader", fail_configuration)
    for index in range(4):
        with recovery_read_scope(f"agent:main:webchat:opening-{index}",
                                 deadline=time.monotonic() + 1, workload="identity"):
            with pytest.raises(RuntimeError, match="synthetic close failure"):
                await storage.get_session("agent:main:webchat:healthy")
    pool = storage._recovery_read_pool
    assert pool is not None
    assert (pool.physical_count, pool.active_count, pool.retiring_count) == (4, 4, 4)
    assert len(pool._quarantined) == 4 and not pool._idle
    for index in range(100):
        key = "agent:main:webchat:opening-0" if index % 2 else "agent:main:webchat:new"
        with recovery_read_scope(key, deadline=time.monotonic() + 1, workload="identity"):
            with pytest.raises(StorageBusyError) as caught:
                await storage.get_session(key)
            assert caught.value.stage == "permit"
    assert len(opened) == 4
    for reader in opened:
        async with reader.execute("SELECT 1") as cursor:
            assert (await cursor.fetchone())[0] == 1
    # Failed recovery setup never poisons the unrelated writer/legacy reader.
    node = await storage.get_session("agent:main:webchat:healthy")
    assert node is not None
    await storage.upsert_session(node)
    await pool.close()
    assert pool.physical_count == pool.active_count == 0
    assert not pool._quarantined
    for reader in opened:
        with pytest.raises((ValueError, aiosqlite.ProgrammingError)):
            async with reader.execute("SELECT 1"):
                pass


async def test_cancelled_native_initialization_holds_reader_until_settled(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = NativeGate()
    initialize = storage._initialize_recovery_reader

    async def initialize_slow(reader: Any) -> None:
        await initialize(reader)
        await reader.create_function("recovery_test_gate", 0, gate.block)
        async with reader.execute("SELECT recovery_test_gate()") as cursor:
            await cursor.fetchone()

    monkeypatch.setattr(storage, "_initialize_recovery_reader", initialize_slow)
    key = "agent:main:webchat:healthy"
    with recovery_read_scope(key, deadline=time.monotonic() + 5) as budget:
        pending = asyncio.create_task(storage.get_session(key))
    try:
        await gate.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 0.5)
        pool = storage._recovery_read_pool
        assert pool is not None
        assert (pool.physical_count, pool.active_count, pool.retiring_count) == (1, 1, 1)
        with recovery_read_scope(key, deadline=time.monotonic() + 1):
            with pytest.raises(StorageBusyError):
                await storage.get_session(key)
        assert not gate.release.is_set()
    finally:
        gate.release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await asyncio.wait_for(budget.drain(), 2)
    assert pool.physical_count == pool.active_count == 0
    assert not pool._idle


async def test_concurrent_close_is_serial_and_cancellation_keeps_physical_ownership(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with recovery_read_scope("agent:main:webchat:healthy", deadline=time.monotonic() + 1):
        assert await storage.get_session("agent:main:webchat:healthy")
    pool = storage._recovery_read_pool
    assert pool is not None
    reader = pool._idle[0]
    original = reader.close
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def slow_close() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(reader, "close", slow_close)
    first = asyncio.create_task(storage.close())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(storage.close())
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        # A cancelled lock waiter cannot cancel the one physical closer.
        waiter = asyncio.create_task(pool.close())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not first.done() and not second.done()
        assert calls == 1 and pool.physical_count == 1 and pool._idle == [reader]
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(first, *([second] if second else [])), 2)
    assert calls == 1 and pool.physical_count == 0 and not pool._idle
    assert storage._conn is None and storage._transcript_reader is None
