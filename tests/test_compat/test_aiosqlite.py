from __future__ import annotations

import importlib

import pytest

from opensquilla.compat import aiosqlite


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
