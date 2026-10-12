from __future__ import annotations

import asyncio
import gc
import importlib
import sqlite3
import threading
import warnings
import weakref
from typing import Any

import pytest

from opensquilla.artifact_session.repository import _fetchone
from opensquilla.compat import aiosqlite
from opensquilla.session.storage import SessionStorage


def _use_backend(monkeypatch, backend: str) -> None:
    if backend == "native" and aiosqlite._native_aiosqlite is None:
        pytest.skip("native aiosqlite unavailable")
    monkeypatch.setattr(aiosqlite, "_FORCE_SQLITE3_FALLBACK", False)
    monkeypatch.setattr(aiosqlite, "_prefer_native", backend == "native")


def _cancel_tasks(task, controller, scope: str, message: str) -> None:
    targets = asyncio.all_tasks() - {controller} if scope == "all_tasks" else {task}
    for target in targets:
        target.cancel(message)


def _connection_worker(conn):
    return conn if isinstance(conn, threading.Thread) else getattr(conn, "_thread", None)


async def _close_connection(conn) -> None:
    worker = _connection_worker(conn)
    await conn.close()
    if worker is not None:
        await asyncio.to_thread(worker.join, 2)
        assert not worker.is_alive()


@pytest.mark.parametrize("backend", ["native", "sqlite3"])
@pytest.mark.parametrize("cancellation_scope", ["caller", "all_tasks"])
async def test_completed_cursor_cancellation_closes_snapshot_with_traceback_retained(
    tmp_path, monkeypatch, backend, cancellation_scope,
) -> None:
    _use_backend(monkeypatch, backend)
    database = tmp_path / "synthetic.sqlite"
    storage = await SessionStorage.open(str(database))
    controller = asyncio.current_task()
    reader = None
    retained_cancellations = []
    injected = False
    try:
        async with storage._write_transaction("synthetic_setup") as conn:
            async with conn.execute("CREATE TABLE synthetic_rows (synthetic_value INTEGER)"):
                pass
            async with conn.execute("INSERT INTO synthetic_rows VALUES (1), (2)"):
                pass

        def cancel_with_completed_result(result: Any) -> None:
            nonlocal injected
            if (
                not injected and isinstance(result, sqlite3.Cursor)
                and result.description and result.description[0][0] == "synthetic_value"
            ):
                injected = True
                assert reader is not None
                _cancel_tasks(
                    reader, controller, cancellation_scope, "completed cursor cancellation",
                )

        with monkeypatch.context() as injection:
            if backend == "native":
                core = importlib.import_module("aiosqlite.core")
                set_result = core.set_result

                def completed(future, result):
                    set_result(future, result)
                    cancel_with_completed_result(result)

                injection.setattr(core, "set_result", completed)
            else:
                loop = asyncio.get_running_loop()
                run_in_executor = loop.run_in_executor

                def executor(*args):
                    future = run_in_executor(*args)

                    def completed(work):
                        if not work.cancelled() and work.exception() is None:
                            cancel_with_completed_result(work.result())

                    future.add_done_callback(completed)
                    return future

                injection.setattr(loop, "run_in_executor", executor)

            async def read():
                async with storage.read_transaction("synthetic_cancelled_read") as conn:
                    return await _fetchone(conn, "SELECT synthetic_value FROM synthetic_rows")

            reader = asyncio.create_task(read())
            with pytest.raises(
                asyncio.CancelledError, match="completed cursor cancellation",
            ) as caught:
                await reader
            retained_cancellations.append(caught.value)
        assert injected
        assert retained_cancellations[0].__traceback__ is not None
        assert not storage.conn.in_transaction
        writer = sqlite3.connect(database, isolation_level=None)
        try:
            writer.execute("INSERT INTO synthetic_rows VALUES (3)").close()
        finally:
            writer.close()
        async with storage._write_transaction("synthetic_first_write", budget_seconds=0):
            pass
        # Retain the actual exception through subsequent reads and writes;
        # collecting its traceback must not be required to release the snapshot.
        for _ in range(2):
            async with storage.read_transaction("synthetic_subsequent_read") as conn:
                assert (await _fetchone(conn, "SELECT COUNT(*) FROM synthetic_rows"))[0] == 3
            async with storage._write_transaction("synthetic_subsequent_write", budget_seconds=0):
                pass
            assert retained_cancellations[0].__traceback__ is not None
            assert not storage.conn.in_transaction
    finally:
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await storage.close()


def _cursor_operation(conn, operation: str):
    if operation == "cursor":
        return conn.cursor()
    if operation == "execute":
        return conn.execute("SELECT value FROM synthetic_rows")
    if operation == "executemany":
        return conn.executemany("INSERT INTO synthetic_rows VALUES (?)", [(3,)])
    return conn.executescript("SELECT value FROM synthetic_rows;")


@pytest.mark.parametrize("operation", ["execute", "executemany", "executescript", "cursor"])
@pytest.mark.parametrize("failure", [None, "creation", "close"])
@pytest.mark.parametrize("cancellation_scope", ["caller", "all_tasks"])
async def test_native_cursor_creation_drains_repeated_cancellation(
    monkeypatch, operation, failure, cancellation_scope,
) -> None:
    _use_backend(monkeypatch, "native")
    started = threading.Event()
    release_creation = threading.Event()
    close_started = threading.Event()
    release_close = threading.Event()
    events = []
    worker_error = sqlite3.OperationalError("synthetic worker failure")
    armed = False

    class ControlledCursor(sqlite3.Cursor):
        def close(self):
            if armed:
                events.append("close_started")
                close_started.set()
                assert release_close.wait(5)
                super().close()
                events.append("close_finished")
                if failure == "close":
                    raise worker_error
            else:
                super().close()

    class ControlledConnection(sqlite3.Connection):
        def complete(self, create):
            if armed:
                started.set()
                assert release_creation.wait(5)
                if failure == "creation":
                    raise worker_error
            return create()

        def raw_cursor(self):
            return super().cursor(factory=ControlledCursor)

        def cursor(self):
            return self.complete(self.raw_cursor)

        def execute(self, *args):
            return self.complete(lambda: self.raw_cursor().execute(*args))

        def executemany(self, *args):
            return self.complete(lambda: self.raw_cursor().executemany(*args))

        def executescript(self, *args):
            return self.complete(lambda: self.raw_cursor().executescript(*args))

    conn = await aiosqlite.connect(":memory:", factory=ControlledConnection)
    controller = asyncio.current_task()
    task = None
    try:
        async with conn.executescript(
            "CREATE TABLE synthetic_rows (value INTEGER);"
            " INSERT INTO synthetic_rows VALUES (1),(2);",
        ):
            pass
        before_tasks = asyncio.all_tasks()
        armed = True
        task = asyncio.create_task(_cursor_operation(conn, operation))
        assert await asyncio.to_thread(started.wait, 2)
        _cancel_tasks(task, controller, cancellation_scope, "original cancellation")
        await asyncio.sleep(0)
        task.cancel("repeated cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        release_creation.set()
        if failure != "creation":
            assert await asyncio.to_thread(close_started.wait, 2)
            _cancel_tasks(task, controller, cancellation_scope, "cancellation during cursor close")
            await asyncio.sleep(0)
            assert not task.done()
        release_close.set()
        with pytest.raises(asyncio.CancelledError, match="original cancellation") as caught:
            await task
        assert caught.value.__cause__ is (worker_error if failure is not None else None)
        assert events == ([] if failure == "creation" else ["close_started", "close_finished"])
        await asyncio.sleep(0)
        assert asyncio.all_tasks() <= before_tasks
        armed = False
        async with conn.execute("SELECT 1") as cursor:
            assert await cursor.fetchone() == (1,)
    finally:
        release_creation.set()
        release_close.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        armed = False
        await _close_connection(conn)


@pytest.mark.parametrize("backend", ["native", "sqlite3"])
async def test_cursor_context_keeps_await_task_and_context_manager_interfaces(
    tmp_path, monkeypatch, backend,
) -> None:
    _use_backend(monkeypatch, backend)
    conn = await aiosqlite.connect(str(tmp_path / "synthetic.sqlite"), isolation_level=None)
    try:
        conn.row_factory = None
        if backend == "native":
            assert isinstance(conn, aiosqlite._native_aiosqlite.Connection)
            original_execute = conn._execute
            calls = []

            async def execute(*args, **kwargs):
                calls.append(True)
                return await original_execute(*args, **kwargs)

            monkeypatch.setattr(conn, "_execute", execute)
        before_tasks = asyncio.all_tasks()
        if backend == "native":
            async with conn.executescript("CREATE TABLE synthetic_rows (value INTEGER);"):
                pass
        else:
            await conn.executescript("CREATE TABLE synthetic_rows (value INTEGER);")
        cursor = await conn.executemany("INSERT INTO synthetic_rows VALUES (?)", [(1,), (2,)])
        await cursor.close()
        pending = conn.execute("SELECT value FROM synthetic_rows")
        cursor = await (asyncio.create_task(pending) if backend == "native" else pending)
        try:
            assert await cursor.fetchall() == [(1,), (2,)]
        finally:
            await cursor.close()
        async with await conn.cursor() as cursor:
            await cursor.execute("SELECT COUNT(*) FROM synthetic_rows")
            assert await cursor.fetchone() == (2,)
        if backend == "native":
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT COUNT(*) FROM synthetic_rows")
                assert await cursor.fetchone() == (2,)
        await asyncio.sleep(0)
        assert asyncio.all_tasks() <= before_tasks
        if backend == "native":
            assert calls
    finally:
        await _close_connection(conn)


async def test_uncancelled_native_call_preserves_worker_exception(monkeypatch) -> None:
    _use_backend(monkeypatch, "native")
    conn = await aiosqlite.connect(":memory:")
    worker_error = sqlite3.OperationalError("synthetic worker failure")

    def fail():
        raise worker_error

    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            await conn._execute(fail)
        assert caught.value is worker_error
    finally:
        await _close_connection(conn)


@pytest.mark.parametrize("close_failure", [False, True])
async def test_native_guard_failure_closes_connection_before_fallback(monkeypatch, close_failure):
    events = []
    close_error = RuntimeError("synthetic close failure")

    class Connection:
        async def close(self):
            events.append("closed")
            if close_failure:
                raise close_error

    class Native:
        async def connect(self, *_args, **_kwargs):
            events.append("opened")
            return Connection()

    monkeypatch.setattr(aiosqlite, "_native_aiosqlite", Native())
    monkeypatch.setattr(aiosqlite, "_FORCE_SQLITE3_FALLBACK", False)
    monkeypatch.setattr(aiosqlite, "_prefer_native", True)
    if close_failure:
        with pytest.raises(RuntimeError) as caught:
            await aiosqlite.connect(":memory:")
        assert caught.value is close_error
    else:
        conn = await aiosqlite.connect(":memory:")
        try:
            assert isinstance(conn, aiosqlite._AsyncConnection)
            assert aiosqlite._prefer_native is False
        finally:
            await conn.close()
    assert events == ["opened", "closed"]


async def test_native_guard_install_failure_stops_actual_worker_before_fallback(monkeypatch):
    _use_backend(monkeypatch, "native")
    native = aiosqlite._native_aiosqlite
    opened = []

    class Native:
        async def connect(self, *args, **kwargs):
            conn = await native.connect(*args, **kwargs)
            opened.append(conn)
            return conn

    def fail_guard(_conn):
        raise RuntimeError("synthetic guard installation failure")

    monkeypatch.setattr(aiosqlite, "_native_aiosqlite", Native())
    monkeypatch.setattr(aiosqlite, "_guard_native_cursor_creation", fail_guard)
    conn = await aiosqlite.connect(":memory:")
    try:
        assert isinstance(conn, aiosqlite._AsyncConnection)
        assert len(opened) == 1
        assert opened[0]._connection is None
        assert not opened[0]._running
        worker = _connection_worker(opened[0])
        assert worker is not None
        await asyncio.to_thread(worker.join, 2)
        assert not worker.is_alive()
    finally:
        await conn.close()


@pytest.mark.parametrize("operation", ["execute", "executemany", "executescript", "cursor"])
async def test_native_cursor_cancelled_before_first_step_creates_no_inner_coroutine(
    monkeypatch, operation,
) -> None:
    _use_backend(monkeypatch, "native")
    conn = await aiosqlite.connect(":memory:")
    calls = []
    original_execute = conn._execute

    async def execute(*args, **kwargs):
        calls.append(True)
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(conn, "_execute", execute)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            task = asyncio.create_task(_cursor_operation(conn, operation))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            task = None
            gc.collect()
        assert calls == []
        assert not [warning for warning in caught if issubclass(warning.category, RuntimeWarning)]
    finally:
        await _close_connection(conn)


async def test_native_pending_result_retains_owner_without_connection_cycle(monkeypatch) -> None:
    _use_backend(monkeypatch, "native")
    conn = await aiosqlite.connect(":memory:")
    pending = conn.execute("SELECT 1")
    owner = weakref.ref(conn)
    await _close_connection(conn)
    conn = None
    gc.collect()
    assert owner() is not None
    pending.close()
    pending = None
    gc.collect()
    assert owner() is None


@pytest.mark.parametrize("result", [None, 7, ("synthetic",)])
async def test_native_non_cursor_call_drains_all_task_cancellation(monkeypatch, result) -> None:
    _use_backend(monkeypatch, "native")
    conn = await aiosqlite.connect(":memory:")
    started = threading.Event()
    release = threading.Event()
    controller = asyncio.current_task()

    def work():
        started.set()
        assert release.wait(5)
        return result

    task = asyncio.create_task(conn._execute(work))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        _cancel_tasks(task, controller, "all_tasks", "original cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="original cancellation"):
            await task
        async with conn.execute("SELECT 1") as cursor:
            assert await cursor.fetchone() == (1,)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await _close_connection(conn)


@pytest.mark.parametrize(
    "shutdown", ["already_closed", "stopping", "stopping_close", "stop_error", "stop_error_close"],
)
@pytest.mark.parametrize("cancel_stop_future", [False, True])
async def test_cancelled_native_cursor_preserves_stop_completion(
    monkeypatch, shutdown, cancel_stop_future,
) -> None:
    _use_backend(monkeypatch, "native")
    if not hasattr(aiosqlite._native_aiosqlite.Connection, "stop"):
        pytest.skip("native version has no public stop")
    close_started = threading.Event()
    release_close = threading.Event()
    if shutdown == "already_closed":
        release_close.set()
    worker_error = sqlite3.OperationalError("synthetic stop failure")
    fail_close = shutdown.startswith("stop_error")
    attempts = 0

    class ControlledConnection(sqlite3.Connection):
        def close(self):
            nonlocal attempts
            attempts += 1
            close_started.set()
            assert release_close.wait(5)
            if fail_close:
                raise worker_error
            super().close()

    conn = await aiosqlite.connect(
        ":memory:", factory=ControlledConnection, check_same_thread=False,
    )
    physical_connection = conn._connection
    cursor_closes = []
    native_queue = conn._tx

    class RecordingQueue:
        def __getattr__(self, name):
            return getattr(native_queue, name)

        def put_nowait(self, item):
            if isinstance(item, tuple) and len(item) == 2:
                function = getattr(item[1], "func", item[1])
                if (
                    isinstance(getattr(function, "__self__", None), sqlite3.Cursor)
                    and getattr(function, "__name__", None) == "close"
                ):
                    cursor_closes.append(item[0])
            native_queue.put_nowait(item)

    conn._tx = RecordingQueue()
    worker = _connection_worker(conn)
    assert worker is not None
    core = importlib.import_module("aiosqlite.core")
    set_result = core.set_result
    caller = None
    closer = None
    first_stop = None
    duplicate_stop = None
    injected = False
    try:
        async with conn.executescript(
            "CREATE TABLE synthetic_rows (synthetic_value INTEGER);"
            " INSERT INTO synthetic_rows VALUES (1),(2);",
        ):
            pass

        def completed(future, result):
            nonlocal first_stop, injected
            set_result(future, result)
            if (
                not injected and isinstance(result, sqlite3.Cursor)
                and result.description and result.description[0][0] == "synthetic_value"
            ):
                injected = True
                assert caller is not None
                caller.cancel("original cursor cancellation")
                first_stop = conn.stop()
                if cancel_stop_future:
                    first_stop.cancel("external stop cancellation")
                if shutdown == "already_closed":
                    worker.join(2)
                    assert not worker.is_alive()

        monkeypatch.setattr(core, "set_result", completed)
        caller = asyncio.create_task(conn.execute("SELECT synthetic_value FROM synthetic_rows"))
        assert await asyncio.to_thread(close_started.wait, 2)
        assert first_stop is not None
        duplicate_stop = conn.stop()
        if shutdown.endswith("_close"):
            closer = asyncio.create_task(conn.close())
        if shutdown != "already_closed":
            await asyncio.sleep(0)
            assert not caller.done()
            assert conn._connection is not None
            assert attempts == 1
            if closer is not None:
                assert not closer.done()
            caller.cancel("repeated cancellation while stopping")
        release_close.set()
        done, _ = await asyncio.wait({caller}, timeout=2)
        assert caller in done
        with pytest.raises(asyncio.CancelledError, match="original cursor cancellation") as caught:
            await caller
        assert caught.value.__cause__ is (worker_error if fail_close else None)
        if cancel_stop_future:
            assert first_stop.cancelled()
        elif fail_close:
            with pytest.raises(sqlite3.OperationalError) as stopped:
                await asyncio.wait_for(first_stop, 2)
            assert stopped.value is worker_error
        else:
            await asyncio.wait_for(first_stop, 2)
        if fail_close:
            with pytest.raises(sqlite3.OperationalError) as stopped:
                await asyncio.wait_for(duplicate_stop, 2)
            assert stopped.value is worker_error
            if closer is not None:
                with pytest.raises(sqlite3.OperationalError) as closed:
                    await asyncio.wait_for(closer, 2)
                assert closed.value is worker_error
            else:
                assert conn._connection is not None
                assert worker.is_alive()
        else:
            await asyncio.wait_for(duplicate_stop, 2)
            if closer is not None:
                await asyncio.wait_for(closer, 2)
            assert conn._connection is None
            assert attempts == 1
            await asyncio.wait_for(conn.stop(), 2)
            await asyncio.wait_for(conn.close(), 2)
    finally:
        fail_close = False
        release_close.set()
        await asyncio.to_thread(worker.join, 2)
        if not worker.is_alive():
            # A regression can queue cursor.close after the worker's stop.
            # Resolve only that synthetic orphan so the failed test can exit.
            for future in cursor_closes:
                if not future.done():
                    future.set_exception(RuntimeError("synthetic orphan cleanup"))
        for pending in (caller, closer):
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        if worker.is_alive():
            await asyncio.wait_for(conn.stop(), 2)
        await asyncio.to_thread(worker.join, 2)
        assert not worker.is_alive()
        # Native close's existing exception path may drop its connection
        # pointer after a physical close failure. Release this test resource.
        physical_connection.close()


async def test_native_stop_future_cancelled_by_awaiting_task_keeps_worker_completion(monkeypatch):
    _use_backend(monkeypatch, "native")
    if not hasattr(aiosqlite._native_aiosqlite.Connection, "stop"):
        pytest.skip("native version has no public stop")
    started = threading.Event()
    release = threading.Event()

    class ControlledConnection(sqlite3.Connection):
        def close(self):
            started.set()
            assert release.wait(5)
            super().close()

    conn = await aiosqlite.connect(":memory:", factory=ControlledConnection)
    stopped = conn.stop()

    async def wait_for_stop():
        return await stopped

    waiter = asyncio.create_task(wait_for_stop())
    closer = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        waiter.cancel("stop waiter cancelled")
        with pytest.raises(asyncio.CancelledError, match="stop waiter cancelled"):
            await waiter
        assert stopped.cancelled()
        closer = asyncio.create_task(conn.close())
        await asyncio.sleep(0)
        assert not closer.done()
        assert conn._connection is not None
        release.set()
        await asyncio.wait_for(closer, 2)
        assert conn._connection is None
    finally:
        release.set()
        if closer is not None and not closer.done():
            closer.cancel()
            await asyncio.gather(closer, return_exceptions=True)
        await _close_connection(conn)


@pytest.mark.parametrize("cyclic", [False, True])
async def test_native_unclosed_connection_gc_keeps_original_worker_finalization(
    monkeypatch, cyclic,
) -> None:
    _use_backend(monkeypatch, "native")
    if not hasattr(aiosqlite._native_aiosqlite.Connection, "stop"):
        pytest.skip("native version has no public stop")
    conn = await aiosqlite.connect(":memory:")
    if cyclic:
        conn.synthetic_cycle = conn
    worker = _connection_worker(conn)
    owner = weakref.ref(conn)
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always", ResourceWarning)
        conn = None
        gc.collect()
    await asyncio.to_thread(worker.join, 2)
    assert not worker.is_alive()
    gc.collect()
    assert owner() is None


async def test_native_gc_without_event_loop_keeps_stop_queue_passthrough(monkeypatch) -> None:
    _use_backend(monkeypatch, "native")
    if not hasattr(aiosqlite._native_aiosqlite.Connection, "stop"):
        pytest.skip("native version has no public stop")

    def finalization():
        async def open_connection():
            return await aiosqlite.connect(":memory:")

        conn = asyncio.run(open_connection())
        worker = _connection_worker(conn)
        owner = weakref.ref(conn)
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always", ResourceWarning)
            conn = None
            gc.collect()
        worker.join(2)
        gc.collect()
        return worker.is_alive(), owner() is None

    alive, released = await asyncio.to_thread(finalization)
    assert not alive
    assert released
