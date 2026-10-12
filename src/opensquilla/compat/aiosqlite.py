"""Compatibility wrapper around aiosqlite for Python 3.13 runtime reliability.

On environments where ``aiosqlite.connect`` blocks at startup, this module
falls back to ``sqlite3`` executed in ``asyncio.to_thread`` while keeping the
async API shape used by project call sites.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Iterable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, cast

_timeout_raw = os.getenv("OPENSQUILLA_AIOSQLITE_CONNECT_TIMEOUT_SEC", "1.0")
try:
    _AIOSQLITE_TIMEOUT_SECONDS = float(_timeout_raw)
except ValueError:
    _AIOSQLITE_TIMEOUT_SECONDS = 1.0
_FORCE_SQLITE3_FALLBACK = os.getenv("OPENSQUILLA_FORCE_SQLITE3_BACKEND", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

_native_aiosqlite: Any | None = None
try:
    _native_aiosqlite = importlib.import_module("aiosqlite")
except Exception:
    _native_aiosqlite = None


Row = sqlite3.Row
OperationalError: type[BaseException] = sqlite3.OperationalError
ProgrammingError: type[BaseException] = sqlite3.ProgrammingError
IntegrityError: type[BaseException] = sqlite3.IntegrityError
DatabaseError: type[BaseException] = sqlite3.DatabaseError
Error: type[BaseException] = sqlite3.Error


class Cursor(AbstractAsyncContextManager["Cursor"], Protocol):
    @property
    def rowcount(self) -> int: ...

    @property
    def lastrowid(self) -> int | None: ...

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> Cursor: ...

    async def executemany(
        self, sql: str, seq_of_params: Iterable[Iterable[Any]]
    ) -> Cursor: ...

    async def fetchone(self) -> Any: ...

    async def fetchall(self) -> list[Any]: ...

    async def fetchmany(self, size: int | None = None) -> list[Any]: ...

    async def close(self) -> None: ...

    def __aiter__(self) -> AsyncIterator[Any]: ...


class CursorContext(Awaitable[Cursor], AbstractAsyncContextManager[Cursor], Protocol):
    pass


class Connection(AbstractAsyncContextManager["Connection"], Protocol):
    @property
    def row_factory(self) -> Any: ...

    @row_factory.setter
    def row_factory(self, value: Any) -> None: ...

    @property
    def in_transaction(self) -> bool: ...

    def execute(self, sql: str, params: Iterable[Any] = ()) -> CursorContext: ...

    def executemany(
        self, sql: str, seq_of_params: Iterable[Iterable[Any]]
    ) -> CursorContext: ...

    async def executescript(self, script: str) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def close(self) -> None: ...

    async def cursor(self) -> Cursor: ...

    async def enable_load_extension(self, enabled: bool) -> None: ...

    async def load_extension(self, path: str) -> None: ...

    async def create_function(
        self,
        name: str,
        num_params: int,
        func: Callable[..., Any],
        *,
        deterministic: bool = False,
    ) -> None: ...


_native_available = _native_aiosqlite is not None
_prefer_native: bool | None = None


class _AsyncCursor:
    def __init__(self, cursor: sqlite3.Cursor, lock: asyncio.Lock) -> None:
        self._cursor = cursor
        self._lock = lock

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> _AsyncCursor:
        async with self._lock:
            self._cursor = await asyncio.to_thread(self._cursor.execute, sql, tuple(params))
        return self

    async def executemany(
        self, sql: str, seq_of_params: Iterable[Iterable[Any]]
    ) -> _AsyncCursor:
        async with self._lock:
            self._cursor = await asyncio.to_thread(
                self._cursor.executemany,
                sql,
                cast(Any, seq_of_params),
            )
        return self

    async def fetchone(self) -> Any:
        async with self._lock:
            return await asyncio.to_thread(self._cursor.fetchone)

    async def fetchall(self) -> list[Any]:
        async with self._lock:
            return await asyncio.to_thread(self._cursor.fetchall)

    async def fetchmany(self, size: int | None = None) -> list[Any]:
        async with self._lock:
            if size is None:
                return await asyncio.to_thread(self._cursor.fetchmany)
            return await asyncio.to_thread(self._cursor.fetchmany, size)

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._cursor.close)

    async def __aenter__(self) -> _AsyncCursor:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def __aiter__(self) -> _AsyncCursor:
        return self

    async def __anext__(self) -> Any:
        row = await self.fetchone()
        if row is None:
            raise StopAsyncIteration
        return row


class _CursorProxy:
    def __init__(self, cursor_awaitable: Awaitable[_AsyncCursor]) -> None:
        self._cursor_awaitable = cursor_awaitable
        self._cursor: _AsyncCursor | None = None

    def __await__(self) -> Generator[Any, None, _AsyncCursor]:
        return self._cursor_awaitable.__await__()

    async def __aenter__(self) -> _AsyncCursor:
        self._cursor = await self._cursor_awaitable
        return self._cursor

    async def __aexit__(self, *_: object) -> None:
        if self._cursor is not None:
            await self._cursor.close()


class _AsyncConnection:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._locked = asyncio.Lock()

    @property
    def row_factory(self) -> Any:
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._conn.row_factory = value

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    async def _execute(self, sql: str, params: Iterable[Any] = ()) -> _AsyncCursor:
        async with self._locked:
            cursor = await asyncio.to_thread(self._conn.execute, sql, tuple(params))
        return _AsyncCursor(cursor, self._locked)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> _CursorProxy:
        return _CursorProxy(self._execute(sql, params))

    async def _executemany(
        self, sql: str, seq_of_params: Iterable[Iterable[Any]]
    ) -> _AsyncCursor:
        async with self._locked:
            cursor = await asyncio.to_thread(
                self._conn.executemany,
                sql,
                cast(Any, seq_of_params),
            )
        return _AsyncCursor(cursor, self._locked)

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> _CursorProxy:
        return _CursorProxy(self._executemany(sql, seq_of_params))

    async def executescript(self, script: str) -> None:
        async with self._locked:
            await asyncio.to_thread(self._conn.executescript, script)

    async def commit(self) -> None:
        async with self._locked:
            await asyncio.to_thread(self._conn.commit)

    async def rollback(self) -> None:
        async with self._locked:
            await asyncio.to_thread(self._conn.rollback)

    async def close(self) -> None:
        async with self._locked:
            await asyncio.to_thread(self._conn.close)

    async def cursor(self) -> _AsyncCursor:
        async with self._locked:
            cur = await asyncio.to_thread(self._conn.cursor)
        return _AsyncCursor(cur, self._locked)

    async def enable_load_extension(self, enabled: bool) -> None:
        async with self._locked:
            await asyncio.to_thread(self._conn.enable_load_extension, enabled)

    async def load_extension(self, path: str) -> None:
        async with self._locked:
            await asyncio.to_thread(self._conn.load_extension, path)

    async def create_function(
        self,
        name: str,
        num_params: int,
        func: Callable[..., Any],
        *,
        deterministic: bool = False,
    ) -> None:
        async with self._locked:
            await asyncio.to_thread(
                self._conn.create_function,
                name,
                num_params,
                func,
                deterministic=deterministic,
            )

    async def set_trace_callback(
        self,
        handler: Callable[[str], Any] | None,
    ) -> None:
        async with self._locked:
            await asyncio.to_thread(self._conn.set_trace_callback, handler)

    async def __aenter__(self) -> _AsyncConnection:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


async def _probe_native() -> bool:
    native = _native_aiosqlite
    if _FORCE_SQLITE3_FALLBACK or native is None:
        return False
    try:
        conn = await asyncio.wait_for(
            native.connect(":memory:"),
            timeout=_AIOSQLITE_TIMEOUT_SECONDS,
        )
        await conn.close()
        return True
    except Exception:
        return False


async def _get_backend_decision() -> bool:
    global _prefer_native
    if _FORCE_SQLITE3_FALLBACK or not _native_available:
        return False
    if _prefer_native is None:
        _prefer_native = await _probe_native()
    return bool(_prefer_native)


async def _connect_sqlite3(
    db_path: str,
    *,
    timeout: float = 5.0,
    check_same_thread: bool = False,
    detect_types: int = 0,
    isolation_level: str | None = "",
    factory: type[sqlite3.Connection] = sqlite3.Connection,
    uri: bool = False,
    cached_statements: int = 128,
    **kwargs: Any,
) -> _AsyncConnection:
    # Aiosqlite-only kwargs are intentionally ignored for compatibility.
    kwargs.pop("iter_chunk_size", None)
    kwargs.pop("loop", None)
    kwargs.pop("executor", None)
    if kwargs:
        # Keep this layer permissive: unsupported kwargs are ignored.
        kwargs.clear()
    def _open_sqlite3_connection() -> sqlite3.Connection:
        return sqlite3.connect(
            db_path,
            timeout=timeout,
            detect_types=detect_types,
            isolation_level=cast(Any, isolation_level),
            check_same_thread=check_same_thread,
            factory=factory,
            uri=uri,
            cached_statements=cached_statements,
        )

    conn = await asyncio.to_thread(_open_sqlite3_connection)
    conn.row_factory = sqlite3.Row
    return _AsyncConnection(conn)


class _ConnectProxy:
    """Awaitable + async-context-manager wrapper matching aiosqlite.connect."""

    def __init__(self, db_path: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self._db_path = db_path
        self._args = args
        self._kwargs = kwargs
        self._conn: Connection | None = None

    def __await__(self) -> Generator[Any, None, Connection]:
        return _connect_impl(self._db_path, *self._args, **self._kwargs).__await__()

    async def __aenter__(self) -> Connection:
        self._conn = await _connect_impl(self._db_path, *self._args, **self._kwargs)
        return self._conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._conn is None:
            return
        await self._conn.close()


async def _connect_impl(db_path: str, *args: Any, **kwargs: Any) -> Connection:
    """Return an async sqlite connection, with fallback when aiosqlite is unreliable."""
    global _prefer_native  # noqa: PLW0603
    if args:
        raise TypeError("connect() only accepts keyword parameters in this compatibility layer")

    use_native = await _get_backend_decision()
    if use_native:
        native = _native_aiosqlite
        try:
            if native is None:
                raise RuntimeError("native aiosqlite is unavailable")
            return await asyncio.wait_for(
                native.connect(db_path, **kwargs),
                timeout=_AIOSQLITE_TIMEOUT_SECONDS,
            )
        except Exception:
            _prefer_native = False
    return await _connect_sqlite3(db_path, **kwargs)


def connect(db_path: str, *args: Any, **kwargs: Any) -> _ConnectProxy:
    """Return an object usable as either ``await connect()`` or ``async with connect()``."""
    return _ConnectProxy(db_path, args, kwargs)
