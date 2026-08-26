from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.agent_tasks import AgentTaskRegistry


@pytest.mark.asyncio
async def test_replaced_task_done_callback_does_not_remove_current_task() -> None:
    registry = AgentTaskRegistry()
    session_key = "agent:main:webchat:registry-replacement"
    release_replacement = asyncio.Event()

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    async def replacement_work() -> None:
        await release_replacement.wait()

    first = asyncio.create_task(wait_forever())
    replacement = asyncio.create_task(replacement_work())
    registry.register(session_key, first)
    registry.register(session_key, replacement)

    with contextlib.suppress(asyncio.CancelledError):
        await first
    await asyncio.sleep(0)

    assert registry.get(session_key) is replacement

    release_replacement.set()
    await replacement


@pytest.mark.asyncio
async def test_quiesce_sessions_cancels_real_task_and_holds_admission() -> None:
    registry = AgentTaskRegistry()
    session_key = "agent:main:webchat:registry-quiesce"
    task_started = asyncio.Event()
    finalizer_started = asyncio.Event()
    release_finalizer = asyncio.Event()
    quiesce_entered = asyncio.Event()
    release_quiesce = asyncio.Event()

    async def direct_task() -> None:
        try:
            task_started.set()
            await asyncio.Event().wait()
        finally:
            finalizer_started.set()
            await release_finalizer.wait()

    task = asyncio.create_task(direct_task())
    registry.register(session_key, task)
    await task_started.wait()

    async def quiesce() -> None:
        async with registry.quiesce_sessions([session_key]):
            quiesce_entered.set()
            await release_quiesce.wait()

    quiescing = asyncio.create_task(quiesce())
    await finalizer_started.wait()
    assert quiesce_entered.is_set() is False
    assert quiescing.done() is False

    release_finalizer.set()
    await quiesce_entered.wait()
    assert registry.get(session_key) is None

    admission_entered = asyncio.Event()

    async def enter_admission() -> None:
        async with registry.admission(session_key):
            admission_entered.set()

    admitting = asyncio.create_task(enter_admission())
    await asyncio.sleep(0)
    assert admission_entered.is_set() is False

    release_quiesce.set()
    await quiescing
    await admitting


@pytest.mark.asyncio
async def test_quiesce_sessions_drains_replaced_task_cancellation_tail() -> None:
    registry = AgentTaskRegistry()
    session_key = "agent:main:webchat:registry-replaced-tail"
    first_started = asyncio.Event()
    first_finalizer_started = asyncio.Event()
    release_first_finalizer = asyncio.Event()
    replacement_started = asyncio.Event()
    replacement_finalized = asyncio.Event()
    quiesce_entered = asyncio.Event()

    async def first_work() -> None:
        try:
            first_started.set()
            await asyncio.Event().wait()
        finally:
            first_finalizer_started.set()
            while not release_first_finalizer.is_set():
                try:
                    await release_first_finalizer.wait()
                except asyncio.CancelledError:
                    # A second cancellation must not let the registry mistake
                    # this still-running cancellation tail for a drained task.
                    continue

    async def replacement_work() -> None:
        try:
            replacement_started.set()
            await asyncio.Event().wait()
        finally:
            replacement_finalized.set()

    first = asyncio.create_task(first_work())
    registry.register(session_key, first)
    await first_started.wait()

    replacement = asyncio.create_task(replacement_work())
    registry.register(session_key, replacement)
    await first_finalizer_started.wait()
    await replacement_started.wait()

    async def quiesce() -> None:
        async with registry.quiesce_sessions([session_key]):
            quiesce_entered.set()

    quiescing = asyncio.create_task(quiesce())
    try:
        await replacement_finalized.wait()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(quiesce_entered.wait(), timeout=0.05)
        assert quiesce_entered.is_set() is False
        assert quiescing.done() is False

        release_first_finalizer.set()
        await quiescing
        assert quiesce_entered.is_set()
    finally:
        release_first_finalizer.set()
        for task in (first, replacement, quiescing):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, replacement, quiescing, return_exceptions=True)


@pytest.mark.asyncio
async def test_quiesce_sessions_waits_for_direct_task_authority_cleanup_once() -> None:
    registry = AgentTaskRegistry()
    session_key = "agent:main:webchat:registry-authority-cleanup"
    task_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    quiesce_entered = asyncio.Event()
    async def direct_task() -> None:
        task_started.set()
        await asyncio.Event().wait()

    async def terminal_cleanup_impl() -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    terminal_cleanup = AsyncMock(side_effect=terminal_cleanup_impl)
    task = asyncio.create_task(direct_task())
    registry.register(
        session_key,
        task,
        terminal_cleanup=terminal_cleanup,
    )
    await task_started.wait()

    async def quiesce() -> None:
        async with registry.quiesce_sessions([session_key]):
            quiesce_entered.set()

    quiescing = asyncio.create_task(quiesce())
    await cleanup_started.wait()
    assert terminal_cleanup.await_count == 1
    assert quiesce_entered.is_set() is False

    release_cleanup.set()
    await asyncio.gather(quiescing)
    assert quiesce_entered.is_set()
    terminal_cleanup.assert_awaited_once_with()
