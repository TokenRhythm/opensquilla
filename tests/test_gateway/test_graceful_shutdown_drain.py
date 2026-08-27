"""Graceful shutdown drains in-flight tasks without cancelling them.

shutdown(graceful=True) waits for in-flight tasks to finish; a long task
running when shutdown is called must complete before shutdown returns
without being cancelled. When ``graceful_timeout`` elapses the remaining
tasks are cancelled and shutdown still returns cleanly without raising.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import AgentTaskRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_envelope(
    agent_id: str = "agent-drain",
    session_key: str = "agent-drain::sess-1",
) -> RouteEnvelope:
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


# ---------------------------------------------------------------------------
# graceful_shutdown_drains_inflight
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graceful_shutdown_drains_inflight() -> None:
    """Enqueue one long task, call shutdown(graceful=True, timeout=10s).

    The task must complete (not be cancelled) and shutdown must return after
    the task finishes.
    """
    task_duration = 0.3  # 300 ms — long enough to still be running when shutdown is called
    completed: list[str] = []
    was_cancelled: list[bool] = []

    task_started = asyncio.Event()

    async def turn_handler(run: Any) -> None:
        task_started.set()
        try:
            await asyncio.sleep(task_duration)
            completed.append(run.task_id)
        except asyncio.CancelledError:
            was_cancelled.append(True)
            raise

    runtime = TaskRuntime(
        storage=_make_storage(),
        turn_handler=turn_handler,
        max_concurrency=4,
    )

    h = await runtime.enqueue(_make_envelope(), "long task")

    # Wait until the task is actually running before we call shutdown.
    await asyncio.wait_for(task_started.wait(), timeout=5.0)

    # Graceful shutdown: must wait for the 300 ms task to finish.
    result = await runtime.shutdown(graceful=True, graceful_timeout=10.0)

    assert result.clean is True
    assert result.remaining_driver_count == 0
    assert completed == [h.task_id], (
        f"Task did not complete before shutdown returned: completed={completed}"
    )
    assert was_cancelled == [], (
        "Task was cancelled during graceful shutdown — expected drain, not cancel"
    )


# ---------------------------------------------------------------------------
# graceful_timeout fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graceful_shutdown_timeout_fallback_is_clean() -> None:
    """When graceful_timeout expires, remaining tasks are cancelled cleanly.

    shutdown() must return without raising an exception even when tasks are
    still running at the timeout boundary.
    """
    task_started = asyncio.Event()

    async def slow_handler(run: Any) -> None:
        task_started.set()
        # This task will not finish within the tiny graceful_timeout.
        await asyncio.sleep(60)

    runtime = TaskRuntime(
        storage=_make_storage(),
        turn_handler=slow_handler,
        max_concurrency=4,
    )

    await runtime.enqueue(_make_envelope(), "slow task")
    await asyncio.wait_for(task_started.wait(), timeout=5.0)

    # Tiny graceful_timeout so we hit the fallback-to-cancel path.
    # Must not raise.
    result = await runtime.shutdown(
        graceful=True,
        graceful_timeout=0.05,
        timeout=2.0,
    )

    assert result.clean is True
    assert result.remaining_driver_count == 0
