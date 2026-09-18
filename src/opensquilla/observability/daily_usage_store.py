"""Durable daily counters for clients whose conversation storage is temporary.

This database has no sessions or transcripts and performs no runtime recovery.
Its identity and acknowledgement semantics match the Gateway's daily counters.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from opensquilla.paths import native_io_path

_T = TypeVar("_T")
_IDENTITY_KEY = "telemetry.daily_usage_store_id"
_SQLITE_BUSY_TIMEOUT_SECONDS = 5.0


class DailyUsageStore:
    """Persist counters without opening or recovering a conversation database."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(cls, path: str | Path) -> DailyUsageStore:
        store = cls(native_io_path(path))
        store._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        store._path.touch(mode=0o600, exist_ok=True)
        await store._run(store._initialize)
        return store

    async def close(self) -> None:
        # Every operation owns and closes its SQLite connection. Waiting for
        # the lock also drains an operation cancelled while its worker ran.
        async with self._lock:
            self._closed = True

    def _with_connection(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        # Separate client processes can share this sidecar.  The per-instance
        # asyncio lock below cannot serialize those connections, so give
        # SQLite enough time to wait for another writer on Windows.
        connection = sqlite3.connect(self._path, timeout=_SQLITE_BUSY_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                return operation(connection)
        finally:
            connection.close()

    async def _run(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Daily usage store is closed")
            worker = asyncio.create_task(asyncio.to_thread(self._with_connection, operation))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Cancelling an await cannot stop SQLite in its worker thread.
                # Observe completion before releasing ownership or closing.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not worker.cancelled():
                    worker.exception()
                raise

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry_daily_usage (
                day TEXT PRIMARY KEY,
                conversation_turns INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                uploaded_at INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_preferences (
                preference_key TEXT PRIMARY KEY,
                preference_value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )

    async def ensure_daily_usage_store_id(self) -> str:
        def ensure(connection: sqlite3.Connection) -> str:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO runtime_preferences (
                    preference_key, preference_value, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(preference_key) DO NOTHING
                """,
                (_IDENTITY_KEY, secrets.token_hex(16), time.time_ns() // 1_000_000),
            )
            row = connection.execute(
                "SELECT preference_value FROM runtime_preferences WHERE preference_key = ?",
                (_IDENTITY_KEY,),
            ).fetchone()
            if row is None or re.fullmatch(r"[0-9a-f]{32}", str(row[0])) is None:
                raise ValueError("Invalid daily usage store identity")
            return str(row[0])

        return await self._run(ensure)

    async def record_daily_usage(
        self,
        *,
        day: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        cache_write_tokens: int,
        updated_at: int,
    ) -> None:
        def record(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO telemetry_daily_usage (
                    day, conversation_turns, input_tokens, output_tokens,
                    cached_tokens, cache_write_tokens, updated_at, uploaded_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(day) DO UPDATE SET
                    conversation_turns = conversation_turns + 1,
                    input_tokens = input_tokens + excluded.input_tokens,
                    output_tokens = output_tokens + excluded.output_tokens,
                    cached_tokens = cached_tokens + excluded.cached_tokens,
                    cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens,
                    updated_at = excluded.updated_at,
                    uploaded_at = NULL
                """,
                (day, input_tokens, output_tokens, cached_tokens, cache_write_tokens, updated_at),
            )

        await self._run(record)

    async def list_pending_daily_usage(self, *, before_day: str) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                """
                SELECT day, conversation_turns, input_tokens, output_tokens,
                       cached_tokens, cache_write_tokens, updated_at, uploaded_at
                FROM telemetry_daily_usage
                WHERE day < ? AND uploaded_at IS NULL
                ORDER BY day
                """,
                (before_day,),
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._run(read)

    async def mark_daily_usage_uploaded(
        self,
        *,
        day: str,
        uploaded_at: int,
        expected_conversation_turns: int,
    ) -> bool:
        def acknowledge(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                UPDATE telemetry_daily_usage SET uploaded_at = ?
                WHERE day = ? AND conversation_turns = ?
                """,
                (uploaded_at, day, expected_conversation_turns),
            )
            return cursor.rowcount > 0

        return await self._run(acknowledge)
