"""Stop observation can expire without withdrawing the accepted cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import AgentTaskStatus
from tests.test_gateway.test_rpc_sessions import FakeSession, FakeSessionManager, make_ctx
from tests.test_gateway.test_task_runtime_terminal_cleanup import _make_envelope, _make_storage


@pytest.fixture(autouse=True)
def _isolated_approval_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    # Concurrent terminal cleanup binds the queue's async lock to this test's
    # loop. Keep it out of the global singleton used by other test modules.
    from opensquilla.application.approval_queue import ApprovalQueue

    queue = ApprovalQueue(db_path=tmp_path / "approvals.sqlite")
    monkeypatch.setattr("opensquilla.application.approval_queue.get_approval_queue", lambda: queue)
    monkeypatch.setattr("opensquilla.gateway.approval_queue.get_approval_queue", lambda: queue)
    try:
        yield
    finally:
        queue.close()


@pytest.mark.parametrize("scope", ["session", "task"])
@pytest.mark.parametrize("blocked_operation", ["get", "update"])
async def test_cancel_observer_cannot_interrupt_persistence_or_cancel_successor(
    scope: str, blocked_operation: str,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    running = asyncio.Event()
    successor_running = asyncio.Event()
    storage = _make_storage()
    runtime: TaskRuntime

    async def handler(run: Any) -> None:
        (successor_running if run.message == "successor" else running).set()
        await asyncio.Event().wait()

    runtime = TaskRuntime(storage=storage, turn_handler=handler)
    envelope = _make_envelope()
    handle = await runtime.enqueue(envelope, "first")
    await asyncio.wait_for(running.wait(), 2)
    original_get = storage.get_agent_task
    original_update = storage.update_agent_task

    async def get(task_id: str) -> Any:
        task = runtime._tasks.get(task_id)
        if task_id == handle.task_id and task is not None and task.cancel_requested:
            entered.set()
            await release.wait()
        return await original_get(task_id)

    async def update(task_id: str, **kwargs: Any) -> Any:
        if task_id == handle.task_id and "cancellation_requested" in kwargs.get("details", {}):
            entered.set()
            await release.wait()
        return await original_update(task_id, **kwargs)

    if blocked_operation == "get":
        storage.get_agent_task = get
    else:
        storage.update_agent_task = update
    stop = asyncio.create_task(
        runtime.cancel_exact(
            session_key=envelope.session_key, task_id=handle.task_id,
            source="webui_stop", reason="user_abort",
        ) if scope == "task" else runtime.cancel(
            session_key=envelope.session_key, source="webui_stop", reason="user_abort",
        )
    )
    successor = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        stop.cancel()
        await asyncio.sleep(0)
        stop.cancel()  # Repeated observer cancellation must preserve the same child.
        await asyncio.sleep(0)
        assert not stop.done()
        # Exact cancellation holds admission until settlement. Session cancellation
        # allows a new queued task; the frozen Stop must not select that successor.
        successor_enqueue = asyncio.create_task(runtime.enqueue(envelope, "successor"))
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop, 2)
        successor = await asyncio.wait_for(successor_enqueue, 2)
        record = await runtime.wait(handle.task_id, timeout=2)
        assert record.status == AgentTaskStatus.CANCELLED
        assert record.details["cancellation"] == {"source": "webui_stop", "reason": "user_abort"}
        await asyncio.wait_for(successor_running.wait(), 2)
        assert not runtime._tasks[successor.task_id].cancel_requested
    finally:
        release.set()
        await asyncio.gather(stop, return_exceptions=True)
        if successor is not None:
            await runtime.cancel(task_id=successor.task_id)
            await runtime.wait(successor.task_id, timeout=2)
        await runtime.shutdown()


async def test_sixteen_session_abort_timeouts_keep_cancellation_and_report_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real RPC projection and runtime at the observed deadline seam."""
    storage = _make_storage()
    release = asyncio.Event()
    all_persisting = asyncio.Event()
    started: set[str] = set()
    persisting: set[str] = set()
    observer_tasks: list[asyncio.Future[Any]] = []

    async def handler(run: Any) -> None:
        started.add(run.envelope.session_key)
        await asyncio.Event().wait()

    runtime = TaskRuntime(storage=storage, turn_handler=handler, max_concurrency=16)
    sessions = [FakeSession(session_key=f"agent:main:webchat:stop-{i}") for i in range(16)]
    handles = [await runtime.enqueue(_make_envelope(s.session_key), "run") for s in sessions]
    async with asyncio.timeout(3):
        while len(started) != 16:
            await asyncio.sleep(0)
    original_update = storage.update_agent_task

    async def update(task_id: str, **kwargs: Any) -> Any:
        if "cancellation_requested" in kwargs.get("details", {}):
            persisting.add(task_id)
            if len(persisting) == 16:
                all_persisting.set()
            await release.wait()
        return await original_update(task_id, **kwargs)

    storage.update_agent_task = update
    original_bounded = rpc_sessions._await_abort_operation

    async def bounded(awaitable: Any, **kwargs: Any) -> Any:
        if kwargs["operation"] != "cancel_runtime_tasks":
            return await original_bounded(awaitable, **kwargs)
        task = asyncio.ensure_future(awaitable)
        observer_tasks.append(task)
        await asyncio.wait_for(all_persisting.wait(), 3)
        # Run the actual exhausted-observation branch after all 16 accepted
        # operations are inside persistence; no scheduler-speed assumption.
        return await original_bounded(
            task, **{**kwargs, "deadline_at_monotonic": 0.0},
        )

    async def no_cleanup(*_args: Any, **_kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(rpc_sessions, "_await_abort_operation", bounded)
    monkeypatch.setattr(rpc_sessions._GatewayCancellationPorts, "cancel_completion", no_cleanup)
    monkeypatch.setattr(rpc_sessions._GatewayCancellationPorts, "cancel_processes", no_cleanup)
    monkeypatch.setattr(rpc_sessions._GatewayCancellationPorts, "reject_approvals", no_cleanup)
    monkeypatch.setattr(rpc_sessions, "_ABORT_TREE_STABILIZATION_PASSES", 1)
    ctx = make_ctx(session_manager=FakeSessionManager(sessions), task_runtime=runtime)
    try:
        results = await asyncio.gather(*(
            get_dispatcher().dispatch(
                str(i), "chat.abort", {"sessionKey": session.session_key, "scope": "session"}, ctx,
            ) for i, session in enumerate(sessions)
        ))
        assert len(persisting) == 16
        for result in results:
            assert result.ok, result.error
            assert result.payload["aborted"] is False
            assert result.payload["reason"] == "task_cancel_unknown"
        assert all(not task.done() for task in observer_tasks)
        release.set()
        await asyncio.gather(*observer_tasks, return_exceptions=True)
        records = await asyncio.gather(*(runtime.wait(h.task_id, timeout=3) for h in handles))
        assert all(record.status == AgentTaskStatus.CANCELLED for record in records)
    finally:
        release.set()
        await asyncio.gather(*observer_tasks, return_exceptions=True)
        for handle in handles:
            await runtime.cancel(task_id=handle.task_id)
            await runtime.wait(handle.task_id, timeout=3)
        await runtime.shutdown()
