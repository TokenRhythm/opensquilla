from __future__ import annotations

import asyncio
import contextvars
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.paths import native_io_path
from opensquilla.sandbox import run_context, user_grants
from opensquilla.sandbox.run_mode import RunMode


@pytest.fixture
def grant_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    database = tmp_path / "state" / "sandbox_user_grants.sqlite"
    legacy = database.with_suffix(".json")
    native_io_path(database.parent).mkdir(parents=True)
    monkeypatch.setattr(user_grants, "_state_path", lambda: database)
    monkeypatch.setattr(user_grants, "_legacy_state_path", lambda: legacy)
    return database, legacy


async def _resolve(*, mode: str = "full", saved: bool = True, include: bool = True):
    node = SimpleNamespace(
        execution_workspace=None,
        workspace_id=None,
        origin={run_context.RUN_CONTEXT_ORIGIN_KEY: {"run_mode": mode}} if saved else {},
    )
    return await run_context.get_run_context(
        SimpleNamespace(),
        "agent:main:synthetic-worker",
        config=SimpleNamespace(sandbox=SimpleNamespace(run_mode=mode)),
        workspace=None,
        session_node=node,
        include_user_grants=include,
    )


def _write_legacy(path: Path) -> None:
    native_io_path(path).write_text(
        json.dumps({"domains": [{"domain": "example.com", "scope": "workspace"}]}),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("mode", "saved", "phase"),
    [
        ("full", True, "schema"),
        ("full", False, "migration"),
        ("safe", True, "migration"),
        ("safe", False, "schema"),
    ],
)
async def test_grants_initialization_lock_can_be_released_by_event_loop(
    grant_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    saved: bool,
    phase: str,
) -> None:
    database, legacy = grant_paths
    if phase == "migration":
        user_grants.load_user_grants_payload()
    _write_legacy(legacy)
    holder = sqlite3.connect(str(native_io_path(database)))
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")
    real_connect = sqlite3.connect

    def bounded_connect(*args: Any, **kwargs: Any):
        # A regression must fail promptly rather than blocking the loop for the
        # production SQLite busy timeout. The real lock and migration stay intact.
        kwargs["timeout"] = 0.75
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(user_grants.sqlite3, "connect", bounded_connect)
    released = asyncio.Event()

    async def release_on_loop() -> None:
        await asyncio.sleep(0.025)
        holder.rollback()
        released.set()

    release_task = asyncio.create_task(release_on_loop())
    try:
        context = await asyncio.wait_for(_resolve(mode=mode, saved=saved), 2)
        assert released.is_set()
        assert context.run_mode is RunMode(mode)
        assert [grant.domain for grant in context.domains] == ["example.com"]
        assert not native_io_path(legacy).exists()
    finally:
        await release_task
        holder.close()


async def test_existing_wal_grants_remain_readable_during_write_transaction(
    grant_paths: tuple[Path, Path],
) -> None:
    database, _legacy = grant_paths
    user_grants.upsert_domain_grant({"domain": "example.com", "scope": "workspace"})
    holder = sqlite3.connect(str(native_io_path(database)))
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("DELETE FROM sandbox_user_grants")
    try:
        context = await asyncio.wait_for(_resolve(), 2)
        assert holder.in_transaction
        assert [grant.domain for grant in context.domains] == ["example.com"]
    finally:
        holder.rollback()
        holder.close()


@pytest.mark.parametrize("corrupt", [False, True])
async def test_grants_connection_and_context_stay_on_worker(
    grant_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    corrupt: bool,
) -> None:
    database, _legacy = grant_paths
    if corrupt:
        native_io_path(database).write_bytes(b"synthetic invalid SQLite database")
    marker = contextvars.ContextVar("synthetic_grants_worker", default="missing")
    main_thread = threading.get_ident()
    observed: list[tuple[str, int, str]] = []
    real_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            observed.append(("connect", threading.get_ident(), marker.get()))
            super().__init__(*args, **kwargs)

        def execute(self, *args: Any, **kwargs: Any):
            observed.append(("execute", threading.get_ident(), marker.get()))
            return super().execute(*args, **kwargs)

        def close(self) -> None:
            observed.append(("close", threading.get_ident(), marker.get()))
            super().close()

    def tracked_connect(*args: Any, **kwargs: Any):
        return real_connect(*args, factory=TrackedConnection, **kwargs)

    monkeypatch.setattr(user_grants.sqlite3, "connect", tracked_connect)
    token = marker.set("synthetic-context")
    try:
        if corrupt:
            with pytest.raises(sqlite3.DatabaseError):
                await _resolve()
        else:
            await _resolve()
    finally:
        marker.reset(token)
    assert {event for event, _thread, _context in observed} == {"connect", "execute", "close"}
    assert len({thread for _event, thread, _context in observed}) == 1
    assert all(thread != main_thread for _event, thread, _context in observed)
    assert all(context == "synthetic-context" for _event, _thread, context in observed)


async def test_concurrent_first_grant_reads_settle_one_migration_at_a_time(
    grant_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, legacy = grant_paths
    _write_legacy(legacy)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    real_load = user_grants.load_user_grants_payload
    calls: list[int] = []

    def controlled_load():
        calls.append(threading.get_ident())
        if len(calls) == 1:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3)
        return real_load()

    monkeypatch.setattr(run_context, "load_user_grants_payload", controlled_load)
    first = asyncio.create_task(_resolve())
    second = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(_resolve(mode="safe", saved=False))
        await asyncio.sleep(0.025)
        assert len(calls) == 1
        release.set()
        contexts = await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert len(calls) == 2
        assert all(
            [grant.domain for grant in context.domains] == ["example.com"]
            for context in contexts
        )
        assert not native_io_path(legacy).exists()
    finally:
        release.set()
        await asyncio.gather(
            first, *([second] if second is not None else []), return_exceptions=True,
        )


@pytest.mark.parametrize("worker_fails", [False, True])
async def test_grants_cancellation_settles_running_worker_and_skips_queued_read(
    monkeypatch: pytest.MonkeyPatch,
    worker_fails: bool,
) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    calls: list[int] = []
    fail_next = False

    def controlled_load():
        calls.append(threading.get_ident())
        if len(calls) == 1:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3)
            if worker_fails:
                raise sqlite3.OperationalError("synthetic cancelled worker failure")
        if fail_next:
            raise sqlite3.OperationalError("synthetic worker failure")
        return {}

    monkeypatch.setattr(run_context, "load_user_grants_payload", controlled_load)
    running = asyncio.create_task(_resolve())
    queued = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        queued = asyncio.create_task(_resolve())
        await asyncio.sleep(0)
        running.cancel()
        await asyncio.sleep(0)
        running.cancel()
        await asyncio.sleep(0)
        assert not running.done()
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(queued, 1)
        assert len(calls) == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 2)
        assert (await asyncio.wait_for(_resolve(), 1)).run_mode is RunMode.FULL
        fail_next = True
        with pytest.raises(sqlite3.OperationalError, match="synthetic worker failure"):
            await asyncio.wait_for(_resolve(), 1)
        fail_next = False
        assert (await asyncio.wait_for(_resolve(), 1)).run_mode is RunMode.FULL
        assert len(calls) == 4
    finally:
        release.set()
        await asyncio.gather(
            running, *([queued] if queued is not None else []), return_exceptions=True,
        )


async def test_run_context_without_user_grants_does_not_load_or_initialize_storage(
    grant_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _legacy = grant_paths

    def unexpected_load():
        raise AssertionError("user grants must not be loaded")

    monkeypatch.setattr(run_context, "load_user_grants_payload", unexpected_load)
    assert (await _resolve(include=False)).run_mode is RunMode.FULL
    assert not native_io_path(database).exists()


def test_grant_workers_can_run_in_successive_event_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow_load():
        time.sleep(0.02)
        return {}

    monkeypatch.setattr(run_context, "load_user_grants_payload", slow_load)

    async def concurrent_reads() -> None:
        contexts = await asyncio.wait_for(asyncio.gather(_resolve(), _resolve()), 2)
        assert all(context.run_mode is RunMode.FULL for context in contexts)

    asyncio.run(concurrent_reads())
    asyncio.run(concurrent_reads())
