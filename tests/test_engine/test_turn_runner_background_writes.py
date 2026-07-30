"""Exact session fences for TurnRunner's detached durable writes."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from opensquilla.engine.runtime import TurnRunner


class _BlockingCompactionStatusManager:
    def __init__(self) -> None:
        self.started: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}

    async def mark_compaction_flush_receipt_status(
        self,
        session_key: str,
        _compaction_id: str,
        _status: str,
    ) -> bool:
        self.started.setdefault(session_key, asyncio.Event()).set()
        await self.release.setdefault(session_key, asyncio.Event()).wait()
        return True


async def _wait_for(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1.0)


async def test_drain_session_background_writes_waits_for_flush_and_status_tail_only() -> None:
    target_key = "agent:main:test:target-background-writes"
    unrelated_key = "agent:main:test:unrelated-background-writes"
    manager = _BlockingCompactionStatusManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
    )
    target_flush_release = asyncio.Event()
    unrelated_flush_release = asyncio.Event()

    async def flush_when_released(release: asyncio.Event) -> SimpleNamespace:
        await release.wait()
        return SimpleNamespace(result_status="flushed")

    target_flush = asyncio.create_task(flush_when_released(target_flush_release))
    unrelated_flush = asyncio.create_task(flush_when_released(unrelated_flush_release))
    runner._active_pre_compaction_flush_tasks[target_key] = target_flush
    runner._active_pre_compaction_flush_tasks[unrelated_key] = unrelated_flush
    runner._schedule_pre_compaction_flush_status_update(
        target_key,
        "target-compaction",
        "flushed",
        "test.compaction",
    )
    runner._schedule_pre_compaction_flush_status_update(
        unrelated_key,
        "unrelated-compaction",
        "flushed",
        "test.compaction",
    )
    await _wait_for(manager.started.setdefault(target_key, asyncio.Event()))
    await _wait_for(manager.started.setdefault(unrelated_key, asyncio.Event()))

    draining = asyncio.create_task(
        runner.drain_session_background_writes([target_key])
    )
    await asyncio.sleep(0)
    assert draining.done() is False

    target_flush_release.set()
    manager.release.setdefault(target_key, asyncio.Event()).set()
    await asyncio.wait_for(draining, timeout=1.0)

    assert target_flush.done() is True
    assert unrelated_flush.done() is False
    unrelated_status_tasks = runner._pre_compaction_flush_status_tasks[unrelated_key]
    assert any(not task.done() for task in unrelated_status_tasks)

    unrelated_flush_release.set()
    manager.release.setdefault(unrelated_key, asyncio.Event()).set()
    await asyncio.gather(unrelated_flush, *unrelated_status_tasks)


async def test_drain_cancellation_waits_for_flush_writer_thread_without_cancelling_it() -> None:
    session_key = "agent:main:test:cancelled-background-drain"
    runner = TurnRunner(provider_selector=None)
    writer_started = threading.Event()
    release_writer = threading.Event()
    writer_settled = threading.Event()

    def blocking_writer() -> SimpleNamespace:
        writer_started.set()
        release_writer.wait(timeout=2.0)
        writer_settled.set()
        return SimpleNamespace(result_status="flushed")

    flush_task = asyncio.create_task(asyncio.to_thread(blocking_writer))
    runner._active_pre_compaction_flush_tasks[session_key] = flush_task
    for _ in range(100):
        if writer_started.is_set():
            break
        await asyncio.sleep(0.01)
    assert writer_started.is_set()

    draining = asyncio.create_task(
        runner.drain_session_background_writes([session_key])
    )
    try:
        await asyncio.sleep(0)
        draining.cancel()
        await asyncio.sleep(0)
        assert draining.done() is False
        assert flush_task.done() is False
        assert writer_settled.is_set() is False

        draining.cancel()
        await asyncio.sleep(0)
        assert draining.done() is False
        assert flush_task.done() is False

        release_writer.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(draining, timeout=1.0)
        assert writer_settled.is_set() is True
        assert flush_task.done() is True
        assert flush_task.cancelled() is False
    finally:
        release_writer.set()
        await asyncio.gather(flush_task, return_exceptions=True)


async def test_drain_stable_resample_catches_flush_created_after_empty_snapshot() -> None:
    session_key = "agent:main:test:flush-empty-snapshot-handoff"
    runner = TurnRunner(provider_selector=None)
    release_flush = asyncio.Event()
    flush_installed = asyncio.Event()
    flush_tasks: list[asyncio.Task[None]] = []

    async def flush() -> None:
        await release_flush.wait()

    def install_flush() -> None:
        task = asyncio.create_task(flush())
        flush_tasks.append(task)
        runner._active_pre_compaction_flush_tasks[session_key] = task
        flush_installed.set()

    draining = asyncio.create_task(
        runner.drain_session_background_writes([session_key])
    )
    asyncio.get_running_loop().call_soon(install_flush)
    try:
        await _wait_for(flush_installed)
        await asyncio.sleep(0)
        assert draining.done() is False

        release_flush.set()
        await asyncio.wait_for(draining, timeout=1.0)
        assert flush_tasks[0].done() is True
    finally:
        release_flush.set()
        await asyncio.gather(*flush_tasks, return_exceptions=True)
