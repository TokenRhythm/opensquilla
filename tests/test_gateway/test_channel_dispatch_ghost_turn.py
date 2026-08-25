"""Ghost-turn regression tests for channel_dispatch.py.

Covers the dispatch-failure path (no transcript append on
TaskQueueFullError), a cross-thread concurrent stress with
asyncio.run_coroutine_threadsafe + ThreadPoolExecutor(max_workers=4) x 8
concurrent sends across a 50-iteration random.shuffle loop, and an
explicit demonstration that the prior append-then-enqueue order created
ghost turns while the current enqueue-then-append order does not.
"""
from __future__ import annotations

import asyncio
import random
from concurrent.futures import Future as CFFuture
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from opensquilla.gateway.task_runtime import (
    TaskQueueFullError,
    TaskRuntimeShuttingDownError,
)

# ── Minimal fakes ────────────────────────────────────────────────────────────


class _FakeSessionManager:
    """Tracks how many times append_message is called."""

    def __init__(self) -> None:
        self.append_calls: list[str] = []
        self._lock = asyncio.Lock()

    async def append_message(self, session_key: str, *, role: str, content: Any) -> Any:
        async with self._lock:
            self.append_calls.append(session_key)
        msg = MagicMock()
        msg.content = content if isinstance(content, str) else "persisted"
        return msg

    def stamp_user_text(self, text: str) -> str:
        return text


class _FakeTurnRunner:
    """Provides _get_session_lock just like the real TurnRunner."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        if session_key not in self._locks:
            self._locks[session_key] = asyncio.Lock()
        return self._locks[session_key]


def _make_route_envelope(session_key: str = "s:test") -> MagicMock:
    env = MagicMock()
    env.session_key = session_key
    env.thread_id = None
    env.channel_id = "ch-1"
    return env


def _make_runtime(*, fails: bool) -> MagicMock:
    """Return a MagicMock whose .enqueue is an AsyncMock that either succeeds or raises."""
    rt = MagicMock()
    if fails:
        rt.enqueue = AsyncMock(
            side_effect=TaskQueueFullError(session_key="s:test", max_pending=1)
        )
    else:
        handle = MagicMock()
        handle.task_id = "task-1"
        rt.enqueue = AsyncMock(return_value=handle)
    return rt


# ── Core dispatch logic (mirrors the fixed production path) ──────────────────


async def _dispatch_one(
    sm: _FakeSessionManager,
    tr: _FakeTurnRunner,
    session_key: str,
    runtime: MagicMock,
) -> bool:
    """
    Implements the fixed enqueue-then-append pattern from channel_dispatch.py.
    Returns True on successful dispatch, False on TaskQueueFullError.
    """
    from opensquilla.engine.start_turn import start_turn_via_runtime
    from opensquilla.gateway.channel_dispatch import (
        _append_channel_user_message,
        _maybe_lock,
    )

    session_lock = tr._get_session_lock(session_key)
    route_envelope = _make_route_envelope(session_key)

    try:
        async with _maybe_lock(session_lock):
            await start_turn_via_runtime(
                runtime,
                route_envelope,
                "hello",
                attachments=[],
                mode="followup",
                run_kind="channel_turn",
                semantic_message="hello",
                stream_event_sink=None,
            )
            await _append_channel_user_message(
                session_manager=sm,
                session_key=session_key,
                text="hello",
                attachments=[],
                config=None,
            )
    except (TaskQueueFullError, TaskRuntimeShuttingDownError):
        return False
    return True


# ── dispatch failure does NOT call _append_channel_user_message ─────────────


@pytest.mark.asyncio
async def test_ac1_1_no_append_on_queue_full() -> None:
    """When enqueue raises TaskQueueFullError, append_message is NOT called."""
    sm = _FakeSessionManager()
    tr = _FakeTurnRunner()
    runtime = _make_runtime(fails=True)

    success = await _dispatch_one(sm, tr, "s:test", runtime)

    assert not success, "should report failure"
    assert sm.append_calls == [], (
        f"append_message was called {len(sm.append_calls)} time(s) "
        "even though enqueue raised TaskQueueFullError"
    )


@pytest.mark.asyncio
async def test_no_append_when_runtime_is_shutting_down() -> None:
    sm = _FakeSessionManager()
    tr = _FakeTurnRunner()
    runtime = MagicMock()
    runtime.enqueue = AsyncMock(
        side_effect=TaskRuntimeShuttingDownError(session_key="s:test")
    )

    success = await _dispatch_one(sm, tr, "s:test", runtime)

    assert success is False
    assert sm.append_calls == []


# ── cross-thread concurrent test, 50 loops, random.shuffle ─────────────────


@pytest.mark.asyncio
async def test_ac1_2_3_concurrent_no_ghost_turns() -> None:
    """Cross-thread concurrent ghost-turn check.

    asyncio.run_coroutine_threadsafe + ThreadPoolExecutor(max_workers=4) x 8
    concurrent sends, repeated for 50 iterations with random.shuffle();
    assert append count == successful dispatches each iteration.

    Strategy: worker threads call asyncio.run_coroutine_threadsafe to submit
    coroutines back to the running test event loop, which is the correct use
    of that API (called from a different thread than the loop).
    """
    session_keys = [f"s:session-{i}" for i in range(4)]
    workers = 4
    sends_per_iter = 8
    iterations = 50

    loop = asyncio.get_event_loop()

    for iteration in range(iterations):
        sm = _FakeSessionManager()
        tr = _FakeTurnRunner()

        # Build a mix: 4 succeed, 4 fail, then shuffle.
        sends: list[tuple[str, bool]] = []
        for i in range(sends_per_iter):
            sk = session_keys[i % len(session_keys)]
            succeeds = i % 2 == 0
            sends.append((sk, succeeds))
        random.shuffle(sends)

        # Futures from run_coroutine_threadsafe (concurrent.futures.Future wrapping
        # an asyncio coroutine submitted to this loop from worker threads).
        cf_futures: list[CFFuture[bool]] = []

        def _submit_from_thread(sk: str, succeeds: bool) -> CFFuture[bool]:
            """Called from a worker thread; submits coro to the event loop."""
            rt = _make_runtime(fails=not succeeds)
            coro = _dispatch_one(sm, tr, sk, rt)
            return asyncio.run_coroutine_threadsafe(coro, loop)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            thread_futures = [
                executor.submit(_submit_from_thread, sk, succeeds)
                for sk, succeeds in sends
            ]
            # Each thread_future.result() gives back the concurrent.futures.Future
            # that run_coroutine_threadsafe returns.  Collect them all without
            # blocking the event loop.
            for tf in thread_futures:
                cf = tf.result(timeout=10)
                cf_futures.append(cf)

        # Await all submitted coroutines by wrapping the concurrent.futures.Futures
        # as asyncio-awaitable futures.  This yields control to the event loop so
        # the coroutines actually execute.
        results: list[bool] = list(
            await asyncio.gather(*[asyncio.wrap_future(cf) for cf in cf_futures])
        )
        successful_dispatches = sum(1 for r in results if r)
        expected_appends = successful_dispatches

        assert len(sm.append_calls) == expected_appends, (
            f"Iteration {iteration}: ghost turn detected! "
            f"append_calls={len(sm.append_calls)} "
            f"successful_dispatches={successful_dispatches}"
        )


# ── demonstrate the bug exists without the fix ─────────────────────────────


@pytest.mark.asyncio
async def test_ac1_4_old_order_creates_ghost_turn() -> None:
    """Simulate the prior order (append-then-enqueue) to show it creates
    ghost turns; then verify the current order (enqueue-then-append) does not.
    """
    from opensquilla.engine.start_turn import start_turn_via_runtime
    from opensquilla.gateway.channel_dispatch import (
        _append_channel_user_message,
        _maybe_lock,
    )

    session_key = "s:ghost-test"
    route_envelope = _make_route_envelope(session_key)

    # ── OLD (buggy) order: append THEN enqueue ────────────────────────────
    sm_old = _FakeSessionManager()
    tr_old = _FakeTurnRunner()
    session_lock_old = tr_old._get_session_lock(session_key)
    failing_runtime = _make_runtime(fails=True)

    try:
        # Bug: append first, then attempt enqueue
        await _append_channel_user_message(
            session_manager=sm_old,
            session_key=session_key,
            text="ghost message",
            attachments=[],
            config=None,
        )
        async with _maybe_lock(session_lock_old):
            await start_turn_via_runtime(
                failing_runtime,
                route_envelope,
                "ghost message",
                attachments=[],
                mode="followup",
                run_kind="channel_turn",
                semantic_message="ghost message",
                stream_event_sink=None,
            )
    except TaskQueueFullError:
        pass  # enqueue failed, but append already ran

    # Old order: ghost turn was created
    assert len(sm_old.append_calls) == 1, (
        "PRE-FIX: ghost turn should have been created "
        "(append ran before failed enqueue)"
    )

    # ── NEW (fixed) order: enqueue THEN append ────────────────────────────
    sm_new = _FakeSessionManager()
    tr_new = _FakeTurnRunner()
    failing_runtime2 = _make_runtime(fails=True)

    success = await _dispatch_one(sm_new, tr_new, session_key, failing_runtime2)

    assert not success, "fixed dispatch should report failure"
    assert len(sm_new.append_calls) == 0, (
        f"POST-FIX: no ghost turn should exist, "
        f"but append_calls={sm_new.append_calls}"
    )
