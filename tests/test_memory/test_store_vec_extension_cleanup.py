from __future__ import annotations

import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest

from opensquilla.memory import store as store_module
from opensquilla.memory.store import LongTermMemoryStore


class _FakeDb:
    def __init__(self) -> None:
        self.extension_states: list[bool] = []

    async def enable_load_extension(self, enabled: bool) -> None:
        self.extension_states.append(enabled)

    async def load_extension(self, path: str) -> None:
        raise OSError("load failed")


@pytest.mark.asyncio
async def test_probe_vec_disables_extension_loading_after_load_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FakeDb()
    store = LongTermMemoryStore(":memory:")
    store._db = db  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "sqlite_vec", SimpleNamespace(loadable_path=lambda: "vec"))

    await store._probe_vec_extension()

    assert db.extension_states == [True, False]
    assert store.vec_available is False


@pytest.mark.asyncio
async def test_vec_preparation_yields_loop_and_load_stays_on_connection_owner(monkeypatch) -> None:
    started, release = threading.Event(), threading.Event()
    owner_thread = threading.get_ident()
    worker_threads: list[int] = []
    database_threads: list[int] = []

    def prepare() -> str:
        worker_threads.append(threading.get_ident())
        started.set()
        assert release.wait(2), "test did not release extension preparation"
        return "vec-test"

    class Db(_FakeDb):
        async def enable_load_extension(self, enabled: bool) -> None:
            database_threads.append(threading.get_ident())
            await super().enable_load_extension(enabled)

        async def load_extension(self, path: str) -> None:
            database_threads.append(threading.get_ident())
            assert path == "vec-test"

    db = Db()
    store = LongTermMemoryStore(":memory:")
    store._db = db
    monkeypatch.setattr(store_module, "_sqlite_vec_loadable_path", prepare)
    task = asyncio.create_task(store._probe_vec_extension())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        # This continuation must run while synchronous preparation is blocked.
        assert not task.done()
        assert db.extension_states == []
    finally:
        release.set()
        await task
    assert worker_threads and worker_threads[0] != owner_thread
    assert database_threads == [owner_thread] * 3
    assert db.extension_states == [True, False]
    assert store.vec_available is True


@pytest.mark.asyncio
async def test_unavailable_vec_preparation_keeps_fts_fallback_without_db_loading(
    monkeypatch,
) -> None:
    def unavailable() -> str:
        raise ModuleNotFoundError("sqlite_vec unavailable")

    db = _FakeDb()
    store = LongTermMemoryStore(":memory:")
    store._db = db
    store._fts_available = True
    monkeypatch.setattr(store_module, "_sqlite_vec_loadable_path", unavailable)
    await store._probe_vec_extension()
    assert store.vec_available is False
    assert store._fts_available is True
    assert db.extension_states == []


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["cancel", "close"])
async def test_late_vec_preparation_cannot_touch_database_after_cancel_or_close(
    monkeypatch, end
) -> None:
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def prepare() -> str:
        started.set()
        try:
            assert release.wait(2), "test did not release extension preparation"
            return "vec-test"
        finally:
            finished.set()

    db = _FakeDb()
    store = LongTermMemoryStore(":memory:")
    store._db = db
    monkeypatch.setattr(store_module, "_sqlite_vec_loadable_path", prepare)
    task = asyncio.create_task(store._probe_vec_extension())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        if end == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            store._db = None
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        if not task.cancelled():
            await task
    assert db.extension_states == []
    assert store.vec_available is False
