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
