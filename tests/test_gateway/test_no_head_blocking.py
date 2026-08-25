"""No head-session blocking when idle slots are available.

Different session_keys for the same agent run concurrently when global
slots are free — session A holding a slot does NOT block B/C/D from
taking the remaining idle slots. With ``max_concurrency=4`` and one
agent owning four sessions ABCD enqueued simultaneously, all four must
start within 4 seconds rather than serialising behind the first session.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import AgentTaskRecord

# ---------------------------------------------------------------------------
# Helpers (same pattern as test_fair_queuing.py)
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


def test_task_runtime_constructor_defaults_to_eight_slots() -> None:
    async def turn_handler(_run: Any) -> None:
        return None

    runtime = TaskRuntime(storage=_make_storage(), turn_handler=turn_handler)
    assert runtime._max_concurrency == 8


# ---------------------------------------------------------------------------
# no_head_blocking_with_idle_slots
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_head_blocking_with_idle_slots() -> None:
    """max_concurrency=4 + 1 agent + 4 sessions ABCD → all 4 start concurrently.

    Each task sleeps for 1 s.  If head-blocking were present the tasks would
    serialise (total ~4 s); with the fix they all start immediately (total ~1 s).
    We assert all 4 tasks start within 2 s of enqueue to give CI headroom.
    """
    start_deadline = 2.0  # all 4 must have *started* within this many seconds

    agent_id = "agent-no-block"
    started_at: dict[str, float] = {}
    enqueue_time: float = 0.0

    gate = asyncio.Event()  # keeps tasks alive until we release them

    async def turn_handler(run: Any) -> None:
        started_at[run.session_key] = time.monotonic()
        await gate.wait()

    runtime = TaskRuntime(
        storage=_make_storage(),
        turn_handler=turn_handler,
        max_concurrency=4,
        max_pending_per_session=None,
    )

    sessions = [f"{agent_id}::sess-{label}" for label in ("A", "B", "C", "D")]
    envs = [_make_envelope(agent_id, sk) for sk in sessions]

    enqueue_time = time.monotonic()
    handles = []
    for env in envs:
        h = await runtime.enqueue(env, "hello")
        handles.append(h)

    # Give the event loop enough ticks for all 4 tasks to reach their handler.
    deadline = asyncio.get_event_loop().time() + start_deadline
    while len(started_at) < 4 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)

    # Release all tasks so they can finish cleanly.
    gate.set()

    # Wait for completion (with generous timeout).
    for h in handles:
        try:
            await asyncio.wait_for(runtime.wait(h.task_id), timeout=10.0)
        except (TimeoutError, KeyError):
            pass

    assert len(started_at) == 4, (
        f"Only {len(started_at)}/4 sessions started within {start_deadline}s: "
        f"{list(started_at.keys())}"
    )

    # All 4 must have started within START_DEADLINE seconds of the first enqueue.
    last_start = max(started_at.values()) - enqueue_time
    assert last_start <= start_deadline, (
        f"Last session started {last_start:.2f}s after enqueue — head-blocking "
        f"suspected (expected all 4 within {start_deadline}s): {started_at}"
    )


@pytest.mark.asyncio
async def test_staggered_fourth_session_fills_last_idle_slot() -> None:
    """A/B/C already running must not leave D parked behind a running RR head."""

    agent_id = "agent-staggered-slot"
    release_handlers = asyncio.Event()
    started = {
        label: asyncio.Event()
        for label in ("A", "B", "C", "D")
    }

    async def turn_handler(run: Any) -> None:
        label = run.session_key.rsplit("-", 1)[-1]
        started[label].set()
        await release_handlers.wait()

    runtime = TaskRuntime(
        storage=_make_storage(),
        turn_handler=turn_handler,
        max_concurrency=4,
        max_pending_per_session=None,
    )
    handles = []
    try:
        for label in ("A", "B", "C"):
            env = _make_envelope(agent_id, f"{agent_id}::sess-{label}")
            handles.append(await runtime.enqueue(env, f"start {label}"))
            await asyncio.wait_for(started[label].wait(), timeout=1.0)

        assert runtime._global_in_flight == 3

        d_env = _make_envelope(agent_id, f"{agent_id}::sess-D")
        handles.append(await runtime.enqueue(d_env, "start D later"))
        try:
            await asyncio.wait_for(started["D"].wait(), timeout=0.5)
        except TimeoutError:
            d_started_before_release = False
        else:
            d_started_before_release = True
    finally:
        release_handlers.set()
        await asyncio.gather(
            *(runtime.wait(handle.task_id, timeout=5.0) for handle in handles),
            return_exceptions=True,
        )

    assert d_started_before_release, (
        "D did not claim the fourth idle slot while A/B/C were already running"
    )
