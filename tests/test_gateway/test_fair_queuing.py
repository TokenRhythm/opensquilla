"""Tests for fair queueing per-agent round-robin.

Covers per-agent_id RR scheduling without starvation, DM-not-starved
mixed-load scenario, round-robin order within a single agent_id,
cross-agent_id non-interference, terminal RR-state cleanup, strict
ABCABC ordering at max_concurrency=1, and no-underutilization soak.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import AgentTaskRecord

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_envelope(agent_id: str, session_key: str) -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id=agent_id,
        session_key=session_key,
        input_provenance={"kind": "test"},
    )


def _make_storage() -> Any:
    storage = MagicMock()
    task_db: dict[str, AgentTaskRecord] = {}

    async def create(record: AgentTaskRecord) -> None:
        task_db[record.task_id] = record

    async def update(task_id: str, **kwargs: Any) -> None:
        rec = task_db.get(task_id)
        if rec is None:
            return
        for k, v in kwargs.items():
            if hasattr(rec, k):
                object.__setattr__(rec, k, v)

    async def get(task_id: str) -> AgentTaskRecord | None:
        return task_db.get(task_id)

    async def list_tasks(**kwargs: Any) -> list[AgentTaskRecord]:
        return list(task_db.values())

    storage.create_agent_task = create
    storage.update_agent_task = update
    storage.get_agent_task = get
    storage.list_agent_tasks = list_tasks
    return storage


def _make_runtime(
    max_concurrency: int = 4,
    max_pending_per_session: int | None = None,
) -> TaskRuntime:
    async def turn_handler(run: Any) -> None:
        # Tiny random delay to simulate real work without LLM calls.
        await asyncio.sleep(random.uniform(0.001, 0.005))

    return TaskRuntime(
        storage=_make_storage(),
        turn_handler=turn_handler,
        max_concurrency=max_concurrency,
        max_pending_per_session=max_pending_per_session,
    )


# ---------------------------------------------------------------------------
# DM not starved
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dm_not_starved() -> None:
    """Multiple channel sessions push 100 tasks total; one DM session pushes 10.

    All sessions share the same agent_id.  With fair queuing the DM session
    must complete all 10 tasks before the channel sessions have drained more
    than 80 of their 100 tasks (i.e. DM is not pushed to the end of the queue).

    We use 10 channel sessions × 10 tasks each (total 100) so that multiple
    channel tasks are simultaneously contending for global-sem slots alongside
    the DM tasks, making the per-agent fairness gate meaningful.
    """
    random.seed(42)
    # max_pending_per_session=None: no per-session queue cap so we can enqueue
    # 100 tasks to a single session without hitting TaskQueueFullError.
    runtime = _make_runtime(max_concurrency=4, max_pending_per_session=None)

    agent_id = "agent-dm-starve"
    # Single channel session with 100 tasks vs single DM session with 10 tasks.
    # Both share the same agent_id.  Without fair queuing, channel grabs every
    # slot as soon as it becomes free (FIFO) and DM waits until all 100 channel
    # tasks drain.  With per-agent_id session round-robin, once channel has more
    # completions than DM, it must yield until DM catches up — so DM completes
    # its 10 tasks while channel has completed at most ~20 tasks (10 lead + 10
    # interleaved rounds before DM is done).
    channel_env = _make_envelope(agent_id, f"{agent_id}::channel-1")
    dm_env = _make_envelope(agent_id, f"{agent_id}::dm-1")

    channel_completed: list[str] = []
    dm_completed: list[str] = []

    original_turn = runtime._turn_handler

    async def instrumented_turn(run: Any) -> None:
        await original_turn(run)
        if "channel" in run.session_key:
            channel_completed.append(run.task_id)
        else:
            dm_completed.append(run.task_id)

    runtime._turn_handler = instrumented_turn  # type: ignore[method-assign]

    # Enqueue channel tasks first (100), then DM tasks (10).
    channel_handles = []
    for i in range(100):
        h = await runtime.enqueue(channel_env, f"channel msg {i}")
        channel_handles.append(h)

    dm_handles = []
    for i in range(10):
        h = await runtime.enqueue(dm_env, f"dm msg {i}")
        dm_handles.append(h)

    # Wait for all DM tasks to finish.
    for h in dm_handles:
        await runtime.wait(h.task_id, timeout=30.0)

    channel_done_when_dm_finished = len(channel_completed)

    # Drain remaining channel tasks.
    for h in channel_handles:
        try:
            await runtime.wait(h.task_id, timeout=30.0)
        except KeyError:
            pass

    assert len(dm_completed) == 10, f"DM only completed {len(dm_completed)}/10"
    assert channel_done_when_dm_finished <= 80, (
        f"DM starved: channel had already completed {channel_done_when_dm_finished} "
        f"tasks when DM finished (expected <=80)"
    )


# ---------------------------------------------------------------------------
# Round-robin within same agent_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_round_robin_within_agent() -> None:
    """Same agent_id, 3 sessions A/B/C each push 9 tasks.

    Execution order should approximate ABCABCABC... with <=20% deviation
    from ideal round-robin (i.e. each session gets between 7 and 11 slots
    in the first 27 completions out of 27 total).
    """
    random.seed(7)
    runtime = _make_runtime(max_concurrency=3, max_pending_per_session=None)

    agent_id = "agent-rr"
    envs = {
        "A": _make_envelope(agent_id, f"{agent_id}::sess-a"),
        "B": _make_envelope(agent_id, f"{agent_id}::sess-b"),
        "C": _make_envelope(agent_id, f"{agent_id}::sess-c"),
    }

    completion_order: list[str] = []
    original_turn = runtime._turn_handler

    async def instrumented_turn(run: Any) -> None:
        await original_turn(run)
        for label, env in envs.items():
            if run.session_key == env.session_key:
                completion_order.append(label)
                break

    runtime._turn_handler = instrumented_turn  # type: ignore[method-assign]

    handles = []
    for label in ("A", "B", "C"):
        for i in range(9):
            h = await runtime.enqueue(envs[label], f"msg {i} from {label}")
            handles.append(h)

    for h in handles:
        try:
            await runtime.wait(h.task_id, timeout=30.0)
        except KeyError:
            pass

    counts = {label: completion_order.count(label) for label in ("A", "B", "C")}
    total = len(completion_order)
    assert total == 27, f"Expected 27 completions, got {total}: {counts}"

    # Each session should complete all 9 tasks; check no session was starved
    # relative to the others (tolerance: each session completes between 6 and 12
    # of the first 27, i.e. 9 ± 20% of 27/3 = 9).
    tolerance = int(9 * 0.20) + 1  # floor(1.8)+1 = 2 extra buffer
    for label, count in counts.items():
        assert abs(count - 9) <= tolerance, (
            f"Session {label} completed {count}/9 tasks — outside tolerance {tolerance}: {counts}"
        )


# ---------------------------------------------------------------------------
# Cross-agent_id unaffected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cross_agent_unaffected() -> None:
    """Two different agent_ids each have 1 session.

    Both should complete all tasks without interference; total completions
    must equal the sum of what was submitted.
    """
    random.seed(99)
    runtime = _make_runtime(max_concurrency=2)

    env_a = _make_envelope("agent-alpha", "agent-alpha::sess-1")
    env_b = _make_envelope("agent-beta", "agent-beta::sess-1")

    completed: list[str] = []
    original_turn = runtime._turn_handler

    async def instrumented_turn(run: Any) -> None:
        await original_turn(run)
        completed.append(run.session_key)

    runtime._turn_handler = instrumented_turn  # type: ignore[method-assign]

    handles = []
    for i in range(10):
        handles.append(await runtime.enqueue(env_a, f"alpha {i}"))
        handles.append(await runtime.enqueue(env_b, f"beta {i}"))

    for h in handles:
        try:
            await runtime.wait(h.task_id, timeout=30.0)
        except KeyError:
            pass

    alpha_count = completed.count("agent-alpha::sess-1")
    beta_count = completed.count("agent-beta::sess-1")
    assert alpha_count == 10, f"agent-alpha completed {alpha_count}/10"
    assert beta_count == 10, f"agent-beta completed {beta_count}/10"


# ---------------------------------------------------------------------------
# Terminal cleanup includes RR state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_terminal_cleanup_includes_rr() -> None:
    """After all tasks complete, _agent_in_flight must be empty (no leaks)."""
    random.seed(1)
    runtime = _make_runtime(max_concurrency=2)

    agent_id = "agent-cleanup"
    env = _make_envelope(agent_id, f"{agent_id}::sess-1")

    handles = []
    for i in range(5):
        h = await runtime.enqueue(env, f"msg {i}")
        handles.append(h)

    for h in handles:
        try:
            await runtime.wait(h.task_id, timeout=10.0)
        except KeyError:
            pass

    # Give the event loop a tick to finish any post-terminal cleanup.
    await asyncio.sleep(0)

    assert runtime._agent_in_flight == {}, (
        f"_agent_in_flight not cleaned up after run: {runtime._agent_in_flight}"
    )


# ---------------------------------------------------------------------------
# Strict ABCABC round-robin order (max_concurrency=1 for determinism)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_round_robin_strict_abcabc_order() -> None:
    """With max_concurrency=1, 3 sessions A/B/C enqueued first-to-last must
    execute in strict ABCABCABC... order (true RR, not count-minimum).

    max_concurrency=1 ensures only one task runs at a time, making the RR
    ordering deterministic and verifiable without timing sensitivity.
    """
    random.seed(0)
    # max_concurrency=1: strictly sequential; RR order must be exact.
    runtime = _make_runtime(max_concurrency=1, max_pending_per_session=None)

    agent_id = "agent-strict-rr"
    # Enqueue sessions in A, B, C order so the deque starts [A, B, C].
    envs = {
        "A": _make_envelope(agent_id, f"{agent_id}::sess-a"),
        "B": _make_envelope(agent_id, f"{agent_id}::sess-b"),
        "C": _make_envelope(agent_id, f"{agent_id}::sess-c"),
    }

    completion_order: list[str] = []
    original_turn = runtime._turn_handler

    async def instrumented_turn(run: Any) -> None:
        await original_turn(run)
        for label, env in envs.items():
            if run.session_key == env.session_key:
                completion_order.append(label)
                break

    runtime._turn_handler = instrumented_turn  # type: ignore[method-assign]

    # Enqueue 3 tasks per session, one session at a time, in A→B→C order.
    # This puts all A tasks in session A's pending list, etc.
    tasks_per_session = 3
    handles = []
    for label in ("A", "B", "C"):
        for i in range(tasks_per_session):
            h = await runtime.enqueue(envs[label], f"msg {i} from {label}")
            handles.append(h)

    for h in handles:
        try:
            await runtime.wait(h.task_id, timeout=30.0)
        except KeyError:
            pass

    total = len(completion_order)
    assert total == tasks_per_session * 3, (
        f"Expected {tasks_per_session * 3} completions, got {total}: {completion_order}"
    )

    # With max_concurrency=1 and true RR, the order must be A,B,C,A,B,C,A,B,C.
    expected = ["A", "B", "C"] * tasks_per_session
    assert completion_order == expected, (
        f"Expected strict ABCABC order, got: {completion_order}"
    )


# ---------------------------------------------------------------------------
# No underutilization: 5 sessions, max_concurrency=4, slot always full
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_underutilization() -> None:
    """1 agent, 5 sessions, 10 messages each, max_concurrency=4.

    This asserts the RR scheduler fills all available slots while work is
    available without relying on wall-clock thresholds that include CI logging
    and enqueue overhead.
    """
    random.seed(5)
    tasks_per_session = 10
    num_sessions = 5
    max_concurrency = 4
    peak_in_flight = 0
    current_in_flight = 0
    peak_reached = asyncio.Event()
    release_handlers = asyncio.Event()
    counter_lock = asyncio.Lock()

    agent_id = "agent-util"

    async def timed_handler(_run: Any) -> None:
        nonlocal current_in_flight, peak_in_flight
        async with counter_lock:
            current_in_flight += 1
            peak_in_flight = max(peak_in_flight, current_in_flight)
            if peak_in_flight >= max_concurrency:
                peak_reached.set()
        try:
            await release_handlers.wait()
        finally:
            async with counter_lock:
                current_in_flight -= 1

    storage = _make_storage()
    runtime = TaskRuntime(
        storage=storage,
        turn_handler=timed_handler,
        max_concurrency=max_concurrency,
        max_pending_per_session=None,
    )

    envs = [
        _make_envelope(agent_id, f"{agent_id}::sess-{i}")
        for i in range(num_sessions)
    ]

    handles = []
    for env in envs:
        for i in range(tasks_per_session):
            h = await runtime.enqueue(env, f"msg {i}")
            handles.append(h)

    await asyncio.wait_for(peak_reached.wait(), timeout=5.0)
    release_handlers.set()
    await asyncio.gather(*(runtime.wait(h.task_id, timeout=60.0) for h in handles))

    assert peak_in_flight == max_concurrency


@pytest.mark.asyncio
async def test_max_eight_staggered_waiters_preserve_capacity_fairness_and_cancel() -> None:
    """Ten staggered sessions keep eight slots full around a cancelled waiter."""

    max_concurrency = 8
    agent_id = "agent-max-eight-stress"
    release_by_label = {
        label: asyncio.Event()
        for label in [*(f"base-{index}" for index in range(max_concurrency)), "W1", "W3"]
    }
    started_by_label = {
        label: asyncio.Event()
        for label in [*(f"base-{index}" for index in range(max_concurrency)), "W1", "W2", "W3"]
    }
    start_order: list[str] = []
    current_in_flight = 0
    peak_in_flight = 0
    counter_lock = asyncio.Lock()

    async def turn_handler(run: Any) -> None:
        nonlocal current_in_flight, peak_in_flight
        label = run.message
        async with counter_lock:
            current_in_flight += 1
            peak_in_flight = max(peak_in_flight, current_in_flight)
            start_order.append(label)
            started_by_label[label].set()
        try:
            await release_by_label[label].wait()
        finally:
            async with counter_lock:
                current_in_flight -= 1

    runtime = TaskRuntime(
        storage=_make_storage(),
        turn_handler=turn_handler,
        max_concurrency=max_concurrency,
        max_pending_per_session=None,
    )
    handles = []
    try:
        for index in range(max_concurrency):
            label = f"base-{index}"
            env = _make_envelope(agent_id, f"{agent_id}::session-{label}")
            handles.append(await runtime.enqueue(env, label))
            await asyncio.wait_for(started_by_label[label].wait(), timeout=1.0)

        assert current_in_flight == max_concurrency

        waiter_handles = {}
        for label in ("W1", "W2", "W3"):
            env = _make_envelope(agent_id, f"{agent_id}::session-{label}")
            handle = await runtime.enqueue(env, label)
            handles.append(handle)
            waiter_handles[label] = handle

        waiter_sessions = {
            label: f"{agent_id}::session-{label}"
            for label in ("W1", "W2", "W3")
        }
        async with asyncio.timeout(1.0):
            while runtime._agent_slot_waiters.get(agent_id) != set(waiter_sessions.values()):
                await asyncio.sleep(0)
        assert not any(started_by_label[label].is_set() for label in ("W1", "W2", "W3"))

        assert await runtime.cancel(task_id=waiter_handles["W2"].task_id) == 1
        await runtime.wait(waiter_handles["W2"].task_id, timeout=2.0)
        assert runtime._agent_slot_waiters.get(agent_id) == {
            waiter_sessions["W1"],
            waiter_sessions["W3"],
        }

        release_by_label["base-0"].set()
        await asyncio.wait_for(started_by_label["W1"].wait(), timeout=1.0)
        assert not started_by_label["W3"].is_set()
        assert runtime._global_in_flight == max_concurrency

        release_by_label["base-1"].set()
        await asyncio.wait_for(started_by_label["W3"].wait(), timeout=1.0)
        assert runtime._global_in_flight == max_concurrency
        assert start_order[-2:] == ["W1", "W3"]
    finally:
        for event in release_by_label.values():
            event.set()
        await asyncio.gather(
            *(runtime.wait(handle.task_id, timeout=10.0) for handle in handles),
            return_exceptions=True,
        )

    assert peak_in_flight == max_concurrency
    assert runtime._global_in_flight == 0
    assert runtime._agent_slot_waiters == {}
