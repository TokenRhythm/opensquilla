from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.application import approval_queue
from opensquilla.gateway import boot as boot_module
from opensquilla.gateway import task_runtime as task_runtime_module
from opensquilla.gateway.boot import _make_task_session_lifecycle_listener
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.session_lifecycle import SessionTaskSnapshot, TaskLifecycleEvent
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskStatus, SessionNode, SessionStatus
from opensquilla.session.storage import SessionStorage


@pytest.mark.asyncio
@pytest.mark.parametrize("update_fails", [False, True])
async def test_terminal_projection_noop_still_invalidates_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, update_fails: bool,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    node = SessionNode(session_key="agent:main:webchat:target", status=(
        SessionStatus.RUNNING if update_fails else SessionStatus.DONE
    ))
    await storage.upsert_session(node)
    events = []

    async def emit(key, name, payload):
        events.append((key, name, payload))

    if update_fails:
        async def update(key, *, expected_session_id=None, expected_session_epoch=None, **fields):
            raise TimeoutError("synthetic projection write failure")
        monkeypatch.setattr(manager, "update", update)
    listener = _make_task_session_lifecycle_listener(session_manager=manager, event_emitter=emit)
    try:
        await listener(TaskLifecycleEvent(
            phase="terminal", session_key=node.session_key, task_id="queued-task",
            task_status=AgentTaskStatus.CANCELLED, run_kind="default",
            session_id=node.session_id, session_epoch=node.epoch,
            task_snapshot=SessionTaskSnapshot(running_task_id=None, queued_task_ids=()),
        ))
        assert events == [(node.session_key, "sessions.changed", {
            "schema_version": 1, "key": node.session_key, "reason": "updated",
            "session_id": node.session_id, "epoch": node.epoch,
        })]
        assert (await storage.get_session(node.session_key)).status == node.status
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement", ["running", "done", "recreated", "deleted", "notice-failure"],
)
async def test_terminal_retry_invalidates_without_replaying_old_session_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "retry.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    node = SessionNode(session_key="agent:main:webchat:target", status=SessionStatus.RUNNING)
    await storage.upsert_session(node)
    started, finish, allow_retry = asyncio.Event(), asyncio.Event(), asyncio.Event()
    successor_started, successor_finish = asyncio.Event(), asyncio.Event()
    events, lifecycle = [], []

    async def no_cleanup(*args, **kwargs):
        return 0

    monkeypatch.setattr(approval_queue, "get_approval_queue", lambda: SimpleNamespace(
        expire_pending_for_session_async=no_cleanup,
    ))

    async def handler(run):
        if started.is_set():
            successor_started.set()
            await successor_finish.wait()
            return
        started.set()
        await finish.wait()

    async def emit(key, name, payload):
        events.append((key, name, payload))

    listener = _make_task_session_lifecycle_listener(session_manager=manager, event_emitter=emit)
    async def lifecycle_listener(event):
        lifecycle.append(event)
        await listener(event)

    runtime = TaskRuntime(storage=storage, turn_handler=handler, event_emitter=emit,
                          lifecycle_listener=lifecycle_listener, running_heartbeat_interval_s=None)
    persist = runtime._persist_terminal_update
    committed_writes = 0
    blocked_task_id = None
    async def delayed_persist(*args, **kwargs):
        nonlocal committed_writes
        if args[0] == blocked_task_id and not allow_retry.is_set():
            raise TimeoutError("synthetic terminal write failure")
        await persist(*args, **kwargs)
        if args[0] == blocked_task_id:
            committed_writes += 1
    monkeypatch.setattr(runtime, "_persist_terminal_update", delayed_persist)
    try:
        handle = await runtime.enqueue(RouteEnvelope(
            source_kind=SourceKind.WEB, source_name="test", agent_id="main",
            session_key=node.session_key, session_id=node.session_id, session_epoch=node.epoch,
        ), "synthetic")
        blocked_task_id = handle.task_id
        await asyncio.wait_for(started.wait(), 2)
        driver = runtime._tasks[handle.task_id].asyncio_task
        finish.set()
        await asyncio.wait_for(asyncio.shield(driver), 2)
        assert runtime._terminal_pending_updates
        assert runtime._resident_count == 1
        if replacement == "deleted":
            await storage.delete_session(node.session_key)
        elif replacement == "recreated":
            current = SessionNode(
                session_key=node.session_key, status=SessionStatus.RUNNING, epoch=node.epoch + 1,
            )
            await storage.upsert_session(current)
        else:
            successor = await runtime.enqueue(RouteEnvelope(
                source_kind=SourceKind.WEB, source_name="test", agent_id="main",
                session_key=node.session_key, session_id=node.session_id, session_epoch=node.epoch,
            ), "successor")
            await asyncio.wait_for(successor_started.wait(), 2)
            if replacement == "done":
                successor_driver = runtime._tasks[successor.task_id].asyncio_task
                successor_finish.set()
                await asyncio.wait_for(asyncio.shield(successor_driver), 2)
            current = await storage.get_session(node.session_key)
        notice_attempts = 0
        if replacement == "notice-failure":
            is_current = task_runtime_module.task_session_is_current
            async def fail_first_notice(*args, **kwargs):
                nonlocal notice_attempts
                notice_attempts += 1
                if notice_attempts == 1:
                    assert runtime._terminal_pending_updates[handle.task_id].persisted
                    assert runtime._resident_count == 1  # Only the successor remains resident.
                    assert handle.task_id not in runtime._terminal_fallback_records
                    raise TimeoutError("synthetic identity read failure")
                return await is_current(*args, **kwargs)
            monkeypatch.setattr(task_runtime_module, "task_session_is_current", fail_first_notice)
        before = len(events)
        allow_retry.set()
        await asyncio.wait_for(asyncio.shield(runtime._terminal_retry_task), 3)
        notices = [payload for _, name, payload in events[before:] if name == "sessions.changed"]
        if replacement in {"recreated", "deleted"}:
            assert notices == []
        else:
            assert notices == [{"schema_version": 1, "key": node.session_key, "reason": "updated",
                                "session_id": node.session_id, "epoch": node.epoch}]
        latest = await storage.get_session(node.session_key)
        if replacement == "deleted":
            assert latest is None
        else:
            assert latest.status == current.status
            assert latest.session_id == current.session_id
            assert latest.epoch == current.epoch
        assert runtime._resident_count == (1 if replacement in {"running", "notice-failure"} else 0)
        assert not runtime._terminal_pending_updates
        assert len([
            event for event in lifecycle
            if event.phase == "terminal" and event.task_id == handle.task_id
        ]) == 1
        if replacement != "deleted":
            assert committed_writes == 1
        if replacement == "notice-failure":
            assert notice_attempts == 2
    finally:
        allow_retry.set()
        finish.set()
        successor_finish.set()
        await runtime.shutdown(timeout=2)
        await storage.close()


@pytest.mark.asyncio
async def test_failed_initial_directory_identity_read_uses_notification_only_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "notice.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    node = SessionNode(session_key="agent:main:webchat:notice", status=SessionStatus.RUNNING)
    await storage.upsert_session(node)
    events, lifecycle = [], []

    async def no_cleanup(*args, **kwargs):
        return 0

    monkeypatch.setattr(approval_queue, "get_approval_queue", lambda: SimpleNamespace(
        expire_pending_for_session_async=no_cleanup,
    ))
    update = manager.update
    async def fail_terminal_update(
        key, *, expected_session_id=None, expected_session_epoch=None, **fields,
    ):
        if fields.get("status") == SessionStatus.DONE:
            raise TimeoutError("synthetic session projection failure")
        return await update(key, expected_session_id=expected_session_id,
                            expected_session_epoch=expected_session_epoch, **fields)
    monkeypatch.setattr(manager, "update", fail_terminal_update)

    async def failed_identity_read(*args, **kwargs):
        raise TimeoutError("synthetic directory identity read failure")
    monkeypatch.setattr(boot_module, "task_session_is_current", failed_identity_read)

    async def emit(key, name, payload):
        events.append((key, name, payload))

    listener = _make_task_session_lifecycle_listener(session_manager=manager, event_emitter=emit)
    async def lifecycle_listener(event):
        lifecycle.append(event)
        await listener(event)

    runtime = TaskRuntime(storage=storage, turn_handler=no_cleanup, event_emitter=emit,
                          lifecycle_listener=lifecycle_listener, running_heartbeat_interval_s=None)
    persist = runtime._persist_terminal_update
    committed_writes = 0
    async def counted_persist(*args, **kwargs):
        nonlocal committed_writes
        await persist(*args, **kwargs)
        committed_writes += 1
    monkeypatch.setattr(runtime, "_persist_terminal_update", counted_persist)
    try:
        handle = await runtime.enqueue(RouteEnvelope(
            source_kind=SourceKind.WEB, source_name="test", agent_id="main",
            session_key=node.session_key, session_id=node.session_id, session_epoch=node.epoch,
        ), "synthetic")
        driver = runtime._tasks[handle.task_id].asyncio_task
        await asyncio.wait_for(asyncio.shield(driver), 2)
        assert runtime._terminal_pending_updates[handle.task_id].persisted
        assert runtime._resident_count == 0
        assert not runtime._terminal_resident_holds
        await asyncio.wait_for(asyncio.shield(runtime._terminal_retry_task), 2)
        notices = [
            payload for _, name, payload in events
            if name == "sessions.changed" and payload["reason"] == "updated"
        ]
        assert len(notices) == 1
        assert notices[0]["session_id"] == node.session_id
        assert len([event for event in lifecycle if event.phase == "terminal"]) == 1
        assert committed_writes == 1
        assert not runtime._terminal_pending_updates
        assert (await storage.get_session(node.session_key)).status == SessionStatus.RUNNING
    finally:
        await runtime.shutdown(timeout=2)
        await storage.close()
