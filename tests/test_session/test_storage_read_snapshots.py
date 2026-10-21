"""Committed artifact snapshots must not block provider accounting writes."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from opensquilla.artifact_session import repository as repository_module
from opensquilla.artifact_session.repository import ArtifactSessionRepository
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage, StorageBusyError
from opensquilla.session.usage_ledger import UsageEventStart


async def _value(conn: Any) -> int:
    async with conn.execute("SELECT value FROM snapshot_probe") as cursor:
        return (await cursor.fetchone())[0]


async def _seed(storage: SessionStorage) -> None:
    async with storage._write_transaction("seed") as conn:
        await conn.execute("CREATE TABLE snapshot_probe (value INTEGER)")
        await conn.execute("INSERT INTO snapshot_probe VALUES (1)")


@pytest.mark.asyncio
async def test_artifact_inventory_allows_usage_start_before_read_finishes(tmp_path, monkeypatch):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    entered, release = asyncio.Event(), asyncio.Event()
    original_fetchall = repository_module._fetchall
    inventory = None

    async def held_inventory(conn, query, params=()):
        rows = await original_fetchall(conn, query, params)
        if "FROM artifact_documents" in query:
            assert conn is not storage.conn
            assert conn is not storage._transcript_reader
            entered.set()
            await release.wait()
        return rows

    try:
        session = SessionNode(
            session_key="agent:main:webchat:snapshot", created_at=1, updated_at=1,
        )
        await storage.upsert_session(session)
        repository = await ArtifactSessionRepository.from_session_storage(storage)
        monkeypatch.setattr(repository_module, "_fetchall", held_inventory)
        inventory = asyncio.create_task(repository.list_documents(session_key=session.session_key))
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert storage._operation_holder is None
        event = await asyncio.wait_for(storage.start_usage_event(UsageEventStart(
            event_id="usage-during-inventory", execution_id="execution-during-inventory",
            call_index=0, session_id=session.session_id, started_at_ms=2,
        )), timeout=5)
        assert event.event_id == "usage-during-inventory"
        assert not inventory.done()
        release.set()
        assert await inventory == ()
    finally:
        release.set()
        if inventory is not None:
            await asyncio.gather(inventory, return_exceptions=True)
        await storage.close()


@pytest.mark.asyncio
async def test_committed_snapshot_excludes_uncommitted_writes_and_stays_consistent(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await _seed(storage)
        async with storage._write_transaction("uncommitted") as writer:
            await writer.execute("UPDATE snapshot_probe SET value = 2")
            async with storage.committed_read_transaction("during_write") as reader:
                assert await _value(reader) == 1
            # Reader observation must not overwrite/clear the active writer's diagnostics.
            assert storage._operation_holder[0] == "uncommitted"

        async with storage.committed_read_transaction("snapshot") as reader:
            assert await _value(reader) == 2
            async with storage._write_transaction("parallel", budget_seconds=0) as writer:
                await writer.execute("UPDATE snapshot_probe SET value = 3")
            assert await _value(reader) == 2
        async with storage.committed_read_transaction("next_snapshot") as next_reader:
            assert next_reader is reader
            assert await _value(next_reader) == 3
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_committed_reader_rejects_mutations_and_reuses_after_error(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await _seed(storage)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            async with storage.committed_read_transaction("invalid_write") as reader:
                await reader.execute("UPDATE snapshot_probe SET value = 2")
        assert not reader.in_transaction
        async with storage.committed_read_transaction("after_error") as next_reader:
            assert next_reader is reader
            assert await _value(next_reader) == 1
    finally:
        await storage.close()


class _GatedConnection:
    def __init__(self, conn: Any, method: str) -> None:
        self.conn, self.method = conn, method
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        original = getattr(self.conn, name)
        if name != self.method:
            return original

        async def gated():
            self.entered.set()
            await self.release.wait()
            return await original()

        return gated


@pytest.mark.asyncio
async def test_repeated_cancellation_finishes_rollback_before_reusing_reader(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    entered = asyncio.Event()
    active = following = None
    gate = None
    try:
        await _seed(storage)
        async with storage.committed_read_transaction("open") as raw_reader:
            assert await _value(raw_reader) == 1
        gate = _GatedConnection(raw_reader, "rollback")
        storage._committed_reader = gate

        async def held_read():
            async with storage.committed_read_transaction("cancel") as reader:
                assert await _value(reader) == 1
                entered.set()
                await asyncio.Event().wait()

        async def following_read():
            async with storage.committed_read_transaction("following") as reader:
                return await _value(reader)

        active = asyncio.create_task(held_read())
        await asyncio.wait_for(entered.wait(), timeout=5)
        active.cancel()
        await asyncio.wait_for(gate.entered.wait(), timeout=5)
        following = asyncio.create_task(following_read())
        await asyncio.sleep(0)
        active.cancel()
        await asyncio.sleep(0)
        assert not active.done()
        assert not following.done()
        assert storage._committed_reader_lock.locked()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await active
        assert await asyncio.wait_for(following, timeout=5) == 1
        assert not raw_reader.in_transaction
        assert not storage._poisoned
    finally:
        if gate is not None:
            gate.release.set()
        for task in (active, following):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await storage.close()


@pytest.mark.asyncio
async def test_cancelled_reader_initialization_finishes_before_reuse(tmp_path, monkeypatch):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    entered, release = asyncio.Event(), asyncio.Event()
    original_initialize = storage._initialize_recovery_reader

    async def held_initialize(reader):
        entered.set()
        await release.wait()
        await original_initialize(reader)

    async def read():
        async with storage.committed_read_transaction("initialize"):
            pytest.fail("cancelled initialization must not enter the caller's body")

    monkeypatch.setattr(storage, "_initialize_recovery_reader", held_initialize)
    reading = asyncio.create_task(read())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        reading.cancel()
        await asyncio.sleep(0)
        assert not reading.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await reading
        assert storage._committed_reader is not None
        async with storage.committed_read_transaction("reuse") as reader:
            async with reader.execute("PRAGMA query_only") as cursor:
                assert (await cursor.fetchone())[0] == 1
    finally:
        release.set()
        await asyncio.gather(reading, return_exceptions=True)
        await storage.close()


@pytest.mark.asyncio
async def test_close_waits_for_active_snapshot_cleanup(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    entered = asyncio.Event()
    closing = None

    async def held_read():
        async with storage.committed_read_transaction("active") as reader:
            await reader.execute("SELECT 1")
            entered.set()
            await asyncio.Event().wait()

    reading = asyncio.create_task(held_read())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        raw_reader = storage._committed_reader
        closing = asyncio.create_task(storage.close())
        await asyncio.sleep(0)
        assert not closing.done()
        assert raw_reader.in_transaction
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
        await asyncio.wait_for(closing, timeout=5)
        with pytest.raises((ValueError, sqlite3.ProgrammingError)):
            await raw_reader.execute("SELECT 1")
    finally:
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)
        if closing is not None:
            await closing
        await storage.close()


@pytest.mark.asyncio
async def test_cancelled_close_still_releases_every_connection(tmp_path):
    path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(path))
    async with storage.committed_read_transaction("open") as raw_reader:
        await raw_reader.execute("SELECT 1")
    writer, transcript_reader = storage.conn, storage._transcript_reader
    gate = _GatedConnection(raw_reader, "close")
    storage._committed_reader = gate
    closing = asyncio.create_task(storage.close())
    try:
        await asyncio.wait_for(gate.entered.wait(), timeout=5)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        assert not closing.done()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        for conn in (raw_reader, writer, transcript_reader):
            with pytest.raises((ValueError, sqlite3.ProgrammingError)):
                await conn.execute("SELECT 1")
        assert storage._committed_reader is None
        assert storage._transcript_reader is None
        assert storage._conn is None
        path.rename(tmp_path / "closed.db")
    finally:
        gate.release.set()
        await asyncio.gather(closing, return_exceptions=True)
        await storage.close()


@pytest.mark.asyncio
async def test_reconnect_retires_committed_reader_and_opens_new_generation(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await _seed(storage)
        async with storage.committed_read_transaction("before") as old_reader:
            assert await _value(old_reader) == 1
        old_generation = storage.connection_generation
        await storage.connect()
        assert storage.connection_generation > old_generation
        assert storage._committed_reader is None
        with pytest.raises((ValueError, sqlite3.ProgrammingError)):
            await old_reader.execute("SELECT 1")
        async with storage.committed_read_transaction("after") as new_reader:
            assert new_reader is not old_reader
            assert await _value(new_reader) == 1
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_failed_reader_rollback_retires_only_reader(tmp_path, monkeypatch):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await _seed(storage)
        async with storage.committed_read_transaction("open") as old_reader:
            assert await _value(old_reader) == 1

        async def failed_rollback():
            raise sqlite3.OperationalError("reader rollback failed")

        monkeypatch.setattr(old_reader, "rollback", failed_rollback)
        with pytest.raises(sqlite3.OperationalError, match="reader rollback failed"):
            async with storage.committed_read_transaction("failure") as reader:
                assert await _value(reader) == 1
        assert storage._committed_reader is None
        assert not storage._poisoned
        async with storage._write_transaction("still_writable") as writer:
            await writer.execute("UPDATE snapshot_probe SET value = 2")
        async with storage.committed_read_transaction("reopen") as reader:
            assert reader is not old_reader
            assert await _value(reader) == 2
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_memory_snapshot_retains_serialized_writer_fallback():
    storage = await SessionStorage.open(":memory:")
    try:
        await _seed(storage)
        async with storage.committed_read_transaction("memory") as reader:
            assert reader is storage.conn
            assert storage._committed_reader is None
            assert await _value(reader) == 1
            with pytest.raises(StorageBusyError):
                async with storage._write_transaction("blocked", budget_seconds=0):
                    pytest.fail("memory writer must remain serialized")
    finally:
        await storage.close()
