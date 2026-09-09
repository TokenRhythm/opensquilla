from __future__ import annotations

import asyncio
import contextvars
import importlib
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from opensquilla.compat import aiosqlite


@pytest.mark.parametrize("use_cursor", [False, True], ids=["connection", "cursor"])
@pytest.mark.parametrize("batch_fails", [False, True], ids=["success", "error"])
async def test_cancelled_fallback_batch_finishes_before_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_cursor: bool, batch_fails: bool
) -> None:
    monkeypatch.setattr(aiosqlite, "_FORCE_SQLITE3_FALLBACK", True)
    conn = await aiosqlite.connect(str(tmp_path / "cancelled-batch.db"))
    loop = asyncio.get_running_loop()
    first_inserted = asyncio.Event()
    release_batch = threading.Event()
    batch_finished = threading.Event()

    def rows() -> Iterator[tuple[int]]:
        yield (1,)
        loop.call_soon_threadsafe(first_inserted.set)
        try:
            assert release_batch.wait(timeout=10)
            yield (2,)
            if batch_fails:
                yield (1,)  # A native IntegrityError after the caller has gone away.
        finally:
            batch_finished.set()

    async def insert() -> None:
        target = await conn.cursor() if use_cursor else conn
        await target.executemany("INSERT INTO items VALUES (?)", rows())

    batch: asyncio.Task[None] | None = None
    rollback: asyncio.Task[None] | None = None
    try:
        await conn.execute("CREATE TABLE items (value INTEGER PRIMARY KEY)")
        await conn.commit()
        batch = asyncio.create_task(insert())
        await asyncio.wait_for(first_inserted.wait(), timeout=5)
        batch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(batch, timeout=5)

        rollback = asyncio.create_task(conn.rollback())
        # The writer is stopped between rows, outside SQLite's internal mutex.
        # A rollback must remain queued until the entire native batch finishes.
        done, _ = await asyncio.wait({rollback}, timeout=0.25)
        rollback_overtook_batch = bool(done)
        release_batch.set()
        assert await asyncio.to_thread(batch_finished.wait, 5)
        await asyncio.wait_for(rollback, timeout=5)

        async with conn.execute("SELECT value FROM items") as cursor:
            assert await cursor.fetchall() == [], "cancelled writes escaped the rollback"
        assert not rollback_overtook_batch
        await conn.execute("INSERT INTO items VALUES (3)")
        await conn.commit()
    finally:
        release_batch.set()
        if batch is not None:
            await asyncio.gather(batch, return_exceptions=True)
        if rollback is not None:
            await asyncio.gather(rollback, return_exceptions=True)
        await conn.close()

    # A fresh connection must see only the subsequent successful transaction.
    async with aiosqlite.connect(str(tmp_path / "cancelled-batch.db")) as reader:
        async with reader.execute("SELECT value FROM items") as cursor:
            assert [row[0] for row in await cursor.fetchall()] == [3]


async def test_fallback_preserves_context_and_releases_lock_after_sql_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aiosqlite, "_FORCE_SQLITE3_FALLBACK", True)
    request_id = contextvars.ContextVar("request_id", default="missing")
    async with aiosqlite.connect(":memory:") as conn:
        await conn.create_function("current_request", 0, request_id.get)
        token = request_id.set("request-123")
        try:
            async with conn.execute("SELECT current_request()") as cursor:
                row = await cursor.fetchone()
                assert row is not None and row[0] == "request-123"
        finally:
            request_id.reset(token)
        with pytest.raises(aiosqlite.OperationalError, match="no such table"):
            await conn.execute("SELECT * FROM missing_table")
        async with conn.execute("SELECT 42") as cursor:
            row = await cursor.fetchone()
            assert row is not None and row[0] == 42


@pytest.mark.asyncio
async def test_sqlite3_fallback_execute_supports_await_and_async_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_FORCE_SQLITE3_BACKEND", "1")
    module = importlib.reload(aiosqlite)
    conn = await module.connect(":memory:")
    try:
        await conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        async with conn.execute("INSERT INTO items (name) VALUES (?)", ("alpha",)) as cur:
            inserted_id = cur.lastrowid
        await conn.commit()

        assert inserted_id == 1

        async with conn.execute("SELECT name FROM items") as cur:
            row = await cur.fetchone()

        assert row is not None
        assert row[0] == "alpha"

        await conn.create_function("py_upper", 1, lambda value: value.upper())
        async with conn.execute("SELECT py_upper(name) FROM items") as cur:
            transformed = await cur.fetchone()

        assert transformed is not None
        assert transformed[0] == "ALPHA"

        traced: list[str] = []
        await conn.set_trace_callback(traced.append)
        async with conn.execute("SELECT name FROM items") as cur:
            await cur.fetchone()
        await conn.set_trace_callback(None)

        assert any("SELECT name FROM items" in statement for statement in traced)
        traced_count = len(traced)
        async with conn.execute("SELECT name FROM items") as cur:
            await cur.fetchone()
        assert len(traced) == traced_count
    finally:
        await conn.close()
        monkeypatch.delenv("OPENSQUILLA_FORCE_SQLITE3_BACKEND", raising=False)
        importlib.reload(aiosqlite)
