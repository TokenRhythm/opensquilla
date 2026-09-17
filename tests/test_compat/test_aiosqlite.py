from __future__ import annotations

import asyncio
import contextvars
import importlib
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from opensquilla.compat import aiosqlite


@pytest.mark.parametrize("operation", ["execute", "fetchone"])
@pytest.mark.parametrize("worker_fails", [False, True])
async def test_cancelled_fallback_keeps_connection_locked_until_worker_finishes(
    operation: str, worker_fails: bool,
) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    events: list[str] = []
    worker_error = sqlite3.OperationalError("worker failed")
    context = contextvars.ContextVar("sqlite_operation_context", default="missing")
    context.set("caller")

    def run_operation(*_args):
        assert context.get() == "caller"
        events.append("operation_started")
        loop.call_soon_threadsafe(started.set)
        assert release.wait(10), "test did not release SQLite worker"
        events.append("operation_finished")
        if worker_fails:
            raise worker_error
        return native_cursor if operation == "execute" else (1,)

    native_cursor = SimpleNamespace(
        fetchone=run_operation,
        close=lambda: events.append("cursor_closed"),
    )
    connection = aiosqlite._AsyncConnection(SimpleNamespace(
        execute=run_operation,
        close=lambda: events.append("connection_closed"),
    ))
    cursor = aiosqlite._AsyncCursor(native_cursor, connection._locked)
    work = asyncio.create_task(
        connection._execute("SELECT 1") if operation == "execute" else cursor.fetchone(),
    )
    close = None
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        work.cancel("original cancellation")
        await asyncio.sleep(0)
        work.cancel("repeated cancellation")
        await asyncio.sleep(0)
        assert not work.done()

        close = asyncio.create_task(connection.close())
        await asyncio.sleep(0)
        assert not close.done()
        assert events == ["operation_started"]
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError, match="original cancellation") as caught:
            await work
        if close is not None:
            await close
        else:
            await connection.close()

    expected = ["operation_started", "operation_finished"]
    if operation == "execute" and not worker_fails:
        expected.append("cursor_closed")
    assert events == [*expected, "connection_closed"]
    if worker_fails:
        assert caught.value.__cause__ is worker_error


@pytest.mark.parametrize("operation", ["execute", "executemany", "executescript", "cursor"])
async def test_cancelled_fallback_closes_new_cursor_before_releasing_connection(
    operation: str,
) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_operation = threading.Event()
    release_cleanup = threading.Event()
    events: list[str] = []

    def create_cursor(*_args):
        loop.call_soon_threadsafe(started.set)
        assert release_operation.wait(10), "test did not release SQLite worker"
        return native_cursor

    def close_cursor():
        events.append("cursor_close_started")
        loop.call_soon_threadsafe(cleanup_started.set)
        assert release_cleanup.wait(10), "test did not release cursor cleanup"
        events.append("cursor_close_finished")

    native_cursor = SimpleNamespace(close=close_cursor)
    connection = aiosqlite._AsyncConnection(SimpleNamespace(
        execute=create_cursor,
        executemany=create_cursor,
        executescript=create_cursor,
        cursor=create_cursor,
        close=lambda: events.append("connection_closed"),
    ))
    operations = {
        "execute": lambda: connection._execute("SELECT 1"),
        "executemany": lambda: connection._executemany("SELECT ?", [(1,)]),
        "executescript": lambda: connection.executescript("SELECT 1;"),
        "cursor": connection.cursor,
    }
    work = asyncio.create_task(operations[operation]())
    close = None
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        work.cancel("original cancellation")
        await asyncio.sleep(0)
        release_operation.set()
        await asyncio.wait_for(cleanup_started.wait(), timeout=10)
        work.cancel("cancel during cleanup")
        await asyncio.sleep(0)
        assert not work.done()
        close = asyncio.create_task(connection.close())
        await asyncio.sleep(0)
        assert not close.done()
        assert events == ["cursor_close_started"]
    finally:
        release_operation.set()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError, match="original cancellation"):
            await work
        if close is not None:
            await close
        else:
            await connection.close()
    assert events == ["cursor_close_started", "cursor_close_finished", "connection_closed"]


async def test_fallback_preserves_uncancelled_worker_error() -> None:
    worker_error = sqlite3.OperationalError("worker failed")

    def fail():
        raise worker_error

    with pytest.raises(sqlite3.OperationalError) as caught:
        await aiosqlite._run_sqlite_call(fail)
    assert caught.value is worker_error


async def test_cancelled_fallback_connect_closes_abandoned_connection(monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    closed: list[bool] = []
    native_connection = SimpleNamespace(close=lambda: closed.append(True))

    def connect(*_args, **_kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(10), "test did not release connection open"
        return native_connection

    monkeypatch.setattr(aiosqlite.sqlite3, "connect", connect)
    work = asyncio.create_task(aiosqlite._connect_sqlite3(":memory:"))
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        work.cancel()
        await asyncio.sleep(0)
        assert not work.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await work
    assert closed == [True]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["native", "sqlite3"])
async def test_total_changes_tracks_writes_and_survives_rollback(
    monkeypatch: pytest.MonkeyPatch, backend: str,
) -> None:
    monkeypatch.setattr(aiosqlite, "_FORCE_SQLITE3_FALLBACK", backend == "sqlite3")
    monkeypatch.setattr(aiosqlite, "_prefer_native", backend == "native")
    conn = await aiosqlite.connect(":memory:")
    try:
        if backend == "native":
            assert isinstance(conn, aiosqlite._native_aiosqlite.Connection)
        else:
            assert isinstance(conn, aiosqlite._AsyncConnection)

        assert conn.total_changes == 0
        await conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        async with conn.execute("SELECT COUNT(*) FROM items") as cursor:
            assert (await cursor.fetchone())[0] == 0
        assert conn.total_changes == 0

        await conn.execute("INSERT INTO items VALUES (1, 'alpha')")
        assert conn.total_changes == 1
        await conn.executemany(
            "INSERT INTO items VALUES (?, ?)", [(2, "beta"), (3, "gamma")],
        )
        await conn.commit()
        assert conn.total_changes == 3

        await conn.execute("UPDATE items SET name = 'changed' WHERE id = 1")
        assert conn.total_changes == 4
        await conn.rollback()
        async with conn.execute("SELECT name FROM items WHERE id = 1") as cursor:
            assert (await cursor.fetchone())[0] == "alpha"
        # SQLite counts writes even when their transaction is rolled back.
        assert conn.total_changes == 4

        await conn.execute("DELETE FROM items WHERE id = 2")
        assert conn.total_changes == 5
        await conn.rollback()
        async with conn.execute("SELECT COUNT(*), total_changes() FROM items") as cursor:
            row = await cursor.fetchone()
            assert tuple(row) == (3, 5)
        assert conn.total_changes == 5

        with pytest.raises(AttributeError):
            conn.total_changes = 0
        assert conn.total_changes == 5
    finally:
        await conn.close()
