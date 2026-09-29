"""Real recovery-reader drain, physical close and reconnect under a process watchdog.

Delay injection models slow completion, not a product latency contract. Faults
must fail: skipping native drain, claiming a close without closing, or hanging.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

from opensquilla.compat import aiosqlite
from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.recovery_reads import recovery_read_scope
from opensquilla.session.storage import SessionStorage, StorageBusyError
from tests.helpers.sqlite_process_probe import READY


async def scenario(directory: Path, fallback: bool, delay: float, fault: str) -> None:
    aiosqlite._FORCE_SQLITE3_FALLBACK = fallback
    storage = await SessionStorage.open(str(directory / "recovery.db"))
    await storage.upsert_session(SessionNode(
        session_key="agent:main:webchat:healthy", session_id="healthy",
        agent_id="main", created_at=100, updated_at=100,
    ))
    await storage.append_transcript_entry(TranscriptEntry(
        session_key="agent:main:webchat:healthy", session_id="healthy",
        message_id="message-1", role="user", content="synthetic history", created_at=100,
    ), expected_epoch=0)
    loop = asyncio.get_running_loop()
    native_entered = asyncio.Event()
    release_native = threading.Event()
    closing_started = asyncio.Event()
    writer_closed = False
    writer, legacy_reader = storage.conn, storage._transcript_reader
    real_writer_close = writer.close
    real_legacy_close = legacy_reader.close
    real_open_reader = storage._open_recovery_reader

    def native_gate() -> int:
        loop.call_soon_threadsafe(native_entered.set)
        if not release_native.wait(120):
            raise AssertionError("native gate was not released")
        return 1

    async def open_reader():
        reader = await real_open_reader()
        await reader.create_function("recovery_test_gate", 0, native_gate)
        return reader

    storage._open_recovery_reader = open_reader
    # Probe setup has its own budget; no short read-deadline contract is under
    # test here. The parent separately bounds startup and execution/cleanup.
    with recovery_read_scope("agent:main:webchat:slow", deadline=time.monotonic() + 90) as budget:
        read = asyncio.create_task(storage._read_history_query("SELECT recovery_test_gate()", ()))
    close = None
    pool = None
    real_pool_close = None
    try:
        print("phase=native-read-startup", flush=True)
        await native_entered.wait()
        if fault != "hang-close":
            print(READY, flush=True)
        read.cancel()
        await asyncio.gather(read, return_exceptions=True)
        pool = storage._recovery_read_pool
        assert pool is not None and pool.active_count == 1
        real_pool_close = pool.close

        async def observed_pool_close() -> None:
            closing_started.set()
            if fault != "skip-drain":
                await real_pool_close()

        def assert_native_drained() -> None:
            assert release_native.is_set() and pool.physical_count == 0, (
                "connection close started before native reader drained"
            )

        async def observed_legacy_close() -> None:
            assert_native_drained()
            await real_legacy_close()

        async def observed_writer_close() -> None:
            nonlocal writer_closed
            print("phase=writer-close", flush=True)
            assert_native_drained()
            if fault == "hang-close":
                print(READY, flush=True)
                while True:
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        continue
            if fault == "no-close":
                return
            started = time.monotonic()
            await asyncio.sleep(delay)  # Deliberate latency injection, never readiness.
            await real_writer_close()
            writer_closed = True
            print(f"writer_close_seconds={time.monotonic() - started:.3f}", flush=True)

        pool.close = observed_pool_close
        writer.close = observed_writer_close
        legacy_reader.close = observed_legacy_close
        close = asyncio.create_task(storage.close())
        await closing_started.wait()
        if close.done():
            await close  # Surface a deliberate ordering violation immediately.
        assert pool._closed and not close.done()
        assert not release_native.is_set() and pool.active_count == 1
        with recovery_read_scope("agent:main:webchat:healthy", deadline=time.monotonic() + 1):
            try:
                await storage.get_session("agent:main:webchat:healthy")
            except StorageBusyError:
                pass
            else:
                raise AssertionError("closing pool admitted another recovery read")
        release_native.set()
        await budget.drain()
        await close  # The process watchdog also bounds native close and teardown.
        assert writer_closed, "writer close returned without closing the connection"
        assert pool.physical_count == pool.active_count == 0
        for connection in (writer, legacy_reader):
            try:
                async with connection.execute("SELECT 1"):
                    pass
            except (ValueError, aiosqlite.ProgrammingError):
                pass
            else:
                raise AssertionError("old connection remained usable after close")
        print("phase=reconnect", flush=True)
        await storage.connect()
        assert storage._recovery_read_pool is None
        with recovery_read_scope("agent:main:webchat:healthy", deadline=time.monotonic() + 10):
            assert await storage.get_session("agent:main:webchat:healthy")
            entries = await storage.get_transcript("healthy")
            assert entries[0].content == "synthetic history"
        print("recovery_close_contract=passed", flush=True)
    finally:
        release_native.set()
        await asyncio.gather(read, *([close] if close else []), return_exceptions=True)
        await budget.drain()
        if pool is not None and real_pool_close is not None:
            pool.close = real_pool_close
            await real_pool_close()
        writer.close = real_writer_close
        legacy_reader.close = real_legacy_close
        await storage.close()
        # Saved handles cover failures after SessionStorage detaches ownership.
        await real_writer_close()
        await real_legacy_close()


if __name__ == "__main__":
    asyncio.run(scenario(
        Path(sys.argv[1]), bool(int(sys.argv[2])), float(sys.argv[3]), sys.argv[4],
    ))
