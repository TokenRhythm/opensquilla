"""A completed session-tree Stop must not declare its successor cancelled."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.application.turn_admission import CancelTurn
from opensquilla.application.turn_cancellation import CancellationTiming, TurnCancellation
from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.boot import _make_task_session_lifecycle_listener
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage


@pytest.mark.asyncio
async def test_tree_cancellation_completion_does_not_cancel_a_successor_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(str(tmp_path / "tasks.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    key = "agent:main:webchat:tree-successor"
    session = await manager.create(key)
    envelope = RouteEnvelope(
        source_kind=SourceKind.WEB, source_name="test", agent_id="main",
        session_key=key, session_id=session.session_id, session_epoch=session.epoch,
        input_provenance={"kind": "test"}, metadata={},
    )
    started = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()
    run_count = 0
    events: list[tuple[str, dict[str, Any]]] = []

    async def handler(_run: Any) -> None:
        nonlocal run_count
        run_count += 1
        started[run_count - 1].set()
        await release.wait()

    async def capture(_ctx: Any, _key: str, name: str, payload: dict[str, Any]) -> None:
        events.append((name, payload))

    async def emit(session_key: str, name: str, payload: dict[str, Any]) -> None:
        await rpc_sessions._emit_to_subscribers(ctx, session_key, name, payload)

    runtime = TaskRuntime(
        storage=storage, turn_handler=handler, event_emitter=emit,
        lifecycle_listener=_make_task_session_lifecycle_listener(
            session_manager=manager, event_emitter=emit,
        ),
    )
    ctx = SimpleNamespace(
        session_manager=manager, task_runtime=runtime,
        config=SimpleNamespace(state_dir=tmp_path), subscription_manager=None,
    )
    cancellation = TurnCancellation(
        rpc_sessions._GatewayCancellationPorts(ctx),
        timing=CancellationTiming(), clock=time.monotonic,
    )
    scanned_empty = asyncio.Event()
    release_query = asyncio.Event()
    original_list = storage.list_agent_tasks
    gate_used = False

    async def list_with_barrier(*args: Any, **kwargs: Any) -> Any:
        nonlocal gate_used
        rows = await original_list(*args, **kwargs)
        # The real SQLite query has finished. Admit B before that old result
        # returns to the cancellation scanner; no task rows/events are forged.
        if not gate_used and rows and not any(row.status in {"running", "queued"} for row in rows):
            gate_used = True
            scanned_empty.set()
            await release_query.wait()
        return rows

    cancel_task: asyncio.Task[Any] | None = None
    monkeypatch.setattr(rpc_sessions, "_send_prepared_to_subscribers", capture)
    try:
        first = await runtime.enqueue(envelope, "A", task_id="tree-A")
        await asyncio.wait_for(started[0].wait(), 5)
        monkeypatch.setattr(storage, "list_agent_tasks", list_with_barrier)
        cancel_task = asyncio.create_task(cancellation.cancel(
            CancelTurn(key, "webchat", None, False, "webui_stop"),
        ))
        await asyncio.wait_for(scanned_empty.wait(), 5)
        second = await runtime.enqueue(envelope, "B", task_id="tree-B")
        await asyncio.wait_for(started[1].wait(), 5)
        release_query.set()
        result = await asyncio.wait_for(cancel_task, 5)

        assert result["cancelled_tasks"] == 1
        assert (await runtime.status(first.task_id)).status == "cancelled"
        assert (await runtime.status(second.task_id)).status == "running"
        snapshot = await runtime.session_task_snapshot(key)
        assert snapshot.running_task_id == second.task_id
        assert second.task_id not in snapshot.cancel_requested_task_ids
        assert events[-1][0] == "sessions.changed"
        assert events[-1][1]["reason"] == "cancellation_completed"
        assert "run_status" not in events[-1][1]
        assert "last_task" not in events[-1][1]
        assert any(name == "task.cancelled" and payload.get("task_id") == first.task_id
                   for name, payload in events)
    finally:
        release_query.set()
        release.set()
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        await runtime.shutdown()
        await storage.close()
