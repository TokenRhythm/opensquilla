from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from opensquilla.application.approval_queue import ApprovalQueue


async def _wait_for_thread(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 3), "worker did not reach checkpoint"


@contextmanager
def _blocked_database(path: Path, operation_started: threading.Event):
    locked = threading.Event()
    release = threading.Event()
    forced_release = threading.Event()
    errors: list[BaseException] = []

    def hold_writer() -> None:
        try:
            with sqlite3.connect(path, timeout=3) as connection:
                connection.execute("BEGIN IMMEDIATE")
                locked.set()
                if not operation_started.wait(3) or not release.wait(1):
                    forced_release.set()
                connection.rollback()
        except BaseException as exc:
            errors.append(exc)
            locked.set()

    holder = threading.Thread(target=hold_writer)
    holder.start()
    try:
        yield locked, release, forced_release
    finally:
        operation_started.set()
        release.set()
        holder.join(timeout=5)
        assert not holder.is_alive()
        assert not errors


@pytest.mark.parametrize(
    ("method", "args", "kwargs"),
    [
        ("get_settings", (), {"node_id": "node"}),
        ("set_settings", ("auto-approve",), {"node_id": "node"}),
        ("has_node_settings", ("node",), {}),
        ("get_run_mode", ("session",), {}),
        ("set_run_mode", ("session", "safe"), {}),
        ("get_elevated_mode", ("session",), {}),
        ("set_elevated_mode", ("session", "on"), {}),
    ],
)
async def test_memory_state_does_not_wait_for_sqlite_worker(
    tmp_path, method, args, kwargs,
) -> None:
    path = tmp_path / "approvals.sqlite"
    queue = ApprovalQueue(db_path=str(path))
    approval_id = queue.request("exec", {})
    # An expired claim makes this read perform real cleanup. Empty reads no
    # longer acquire the writer lock.
    queue._conn.execute(
        "UPDATE approval_queue SET claim_token = 'expired', claim_started_at = 0 "
        "WHERE approval_id = ?", (approval_id,),
    )
    queue.set_settings("prompt", allow_patterns=["original"], node_id="node")
    queue.set_run_mode("session", "full")
    sql_started = threading.Event()
    memory_started = threading.Event()
    queue._conn.set_trace_callback(
        lambda sql: sql_started.set() if sql.startswith("BEGIN IMMEDIATE") else None,
    )
    worker = None
    try:
        with _blocked_database(path, memory_started) as (locked, release, forced):
            await _wait_for_thread(locked)
            worker = asyncio.create_task(queue.get_async(approval_id))
            await _wait_for_thread(sql_started)
            memory_started.set()
            result = getattr(queue, method)(*args, **kwargs)
            blocked_on_database = forced.is_set()
            release.set()
            assert (await asyncio.wait_for(worker, 3)).approval_id == approval_id
            assert not blocked_on_database, f"{method} waited for the SQLite lock"

        if method == "get_settings":
            assert result.allow_patterns == ["original"]
            result.allow_patterns.append("local-copy")
            assert queue.get_settings("node").allow_patterns == ["original"]
        elif method == "set_settings":
            assert queue.get_settings("node").mode == "auto-approve"
        elif method == "has_node_settings":
            assert result is True
        elif method == "get_run_mode":
            assert result == "full"
        elif method == "get_elevated_mode":
            assert result == "full"
        else:
            assert queue.get_run_mode("session") == "safe"
    finally:
        if worker is not None:
            await worker
        queue.close()


async def test_sync_database_mutation_still_waits_for_async_database_worker(tmp_path) -> None:
    path = tmp_path / "approvals.sqlite"
    queue = ApprovalQueue(db_path=str(path))
    approval_id = queue.request("exec", {})
    queue._conn.execute(
        "UPDATE approval_queue SET claim_token = 'expired', claim_started_at = 0 "
        "WHERE approval_id = ?", (approval_id,),
    )
    sql_started = threading.Event()
    mutation_started = threading.Event()
    queue._conn.set_trace_callback(
        lambda sql: sql_started.set() if sql.startswith("BEGIN IMMEDIATE") else None,
    )
    reader = None
    mutation = None

    def resolve() -> None:
        mutation_started.set()
        queue.resolve(approval_id, True)

    try:
        with _blocked_database(path, mutation_started) as (locked, release, forced):
            await _wait_for_thread(locked)
            reader = asyncio.create_task(queue.get_async(approval_id))
            await _wait_for_thread(sql_started)
            mutation = asyncio.create_task(asyncio.to_thread(resolve))
            await _wait_for_thread(mutation_started)
            await asyncio.sleep(0.02)
            assert not forced.is_set()
            assert not mutation.done()
            release.set()
            await asyncio.wait_for(asyncio.gather(reader, mutation), 3)
        assert queue.get(approval_id).approved is True
    finally:
        if reader is not None:
            await reader
        if mutation is not None:
            await mutation
        queue.close()


@pytest.mark.parametrize("cancel_all", [False, True])
async def test_cancelled_composite_keeps_owner_until_consumption_commits(tmp_path, cancel_all):
    queue = ApprovalQueue(db_path=str(tmp_path / "cancel.sqlite"))
    approval_id = queue.request("exec", {})
    queue.resolve(approval_id, True)
    entered = threading.Event()
    release = threading.Event()
    committed = threading.Event()

    def consume():
        entered.set()
        assert release.wait(3)
        queue.consume(approval_id)
        committed.set()

    task = asyncio.create_task(queue.run_operation_async(consume))
    try:
        await _wait_for_thread(entered)
        if cancel_all:
            # asyncio.run() cancels every remaining Task during shutdown.
            current = asyncio.current_task()
            for candidate in asyncio.all_tasks():
                if candidate is not current:
                    candidate.cancel()
        else:
            task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert queue._async_lock.locked()
        assert not committed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert committed.is_set()
        assert queue.get(approval_id).consumed
        assert not queue._async_lock.locked()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        queue.close()


async def test_worker_cancellation_exception_does_not_spin_or_keep_owner(tmp_path):
    queue = ApprovalQueue(db_path=str(tmp_path / "worker-cancel.sqlite"))

    def cancelled_worker():
        raise asyncio.CancelledError("worker cancelled")

    try:
        with pytest.raises(asyncio.CancelledError):
            await queue.run_operation_async(cancelled_worker)
        assert not queue._async_lock.locked()
        approval_id = await queue.request_async("exec", {})
        assert (await queue.get_async(approval_id)).approval_id == approval_id
    finally:
        queue.close()
