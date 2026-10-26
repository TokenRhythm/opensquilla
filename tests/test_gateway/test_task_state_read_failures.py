"""Task metadata must distinguish unavailable ledger reads from an empty ledger."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.rpc.registry import RpcContext, RpcRegistry
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.recovery_read_budget import recovery_read_scope
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus
from opensquilla.session.storage import SessionStorage, StorageBusyError


@pytest.fixture
async def task_state_case(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "task-state.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    node = await manager.create("agent:main:webchat:task-state-read")
    record = AgentTaskRecord(session_key=node.session_key, status=AgentTaskStatus.SUCCEEDED)
    await storage.create_agent_task(record)

    async def turn_handler(_run):
        raise AssertionError("Task-state reads must not execute a turn")

    runtime = TaskRuntime(storage=storage, turn_handler=turn_handler)
    context = RpcContext(conn_id="task-state-read", session_manager=manager, task_runtime=runtime)
    try:
        yield SimpleNamespace(storage=storage, manager=manager, node=node, record=record,
                              runtime=runtime, context=context)
    finally:
        await storage.close()


@asynccontextmanager
async def held_read_permit(storage, key):
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        with recovery_read_scope(key, deadline=time.monotonic() + 5) as budget:
            async def wait(_reader):
                entered.set()
                await release.wait()
            try:
                await storage._run_recovery_read("test-held-lease", wait)
            finally:
                await budget.drain()

    task = asyncio.create_task(hold())
    await asyncio.wait_for(entered.wait(), 2)
    try:
        yield
    finally:
        release.set()
        await task


async def test_same_session_permit_busy_is_not_retried_as_empty_task_ledger(
    task_state_case, monkeypatch,
):
    case = task_state_case
    key = case.node.session_key
    fallback = AsyncMock(side_effect=AssertionError("Must not retry the same busy storage"))
    monkeypatch.setattr(case.runtime, "list", fallback)
    async with held_read_permit(case.storage, key):
        with recovery_read_scope(key, deadline=time.monotonic() + 2):
            with pytest.raises(StorageBusyError) as caught:
                await rpc_sessions._list_task_rows(case.context, case.storage, key)
        assert caught.value.operation == "list_recent_agent_tasks"
        assert caught.value.stage == "permit"
        assert caught.value.resource == "session_storage_recovery_read_pool"
        fallback.assert_not_awaited()
    with recovery_read_scope(key, deadline=time.monotonic() + 2):
        rows = await rpc_sessions._list_task_rows(case.context, case.storage, key)
    assert [row.task_id for row in rows] == [case.record.task_id]


async def test_metadata_budget_has_no_internal_same_session_permit_collision(task_state_case):
    """The complete application awaits its task/workspace/planning reads in order."""
    case = task_state_case
    key = case.node.session_key
    with recovery_read_scope(key, deadline=time.monotonic() + 2):
        metadata = await rpc_sessions._handle_sessions_messages_hydrate({"key": key}, case.context)
    assert [task["task_id"] for task in metadata["tasks"]] == [case.record.task_id]
    assert metadata["last_task"]["task_id"] == case.record.task_id
    assert metadata["hydration_complete"]


async def test_metadata_rpc_preserves_initial_busy_operation_and_recovers(task_state_case):
    case = task_state_case
    key = case.node.session_key
    dispatcher = RpcRegistry()
    dispatcher.register("sessions.messages.hydrate", rpc_sessions._handle_sessions_messages_hydrate,
                        scope="operator.read")
    async with held_read_permit(case.storage, key):
        with recovery_read_scope(key, deadline=time.monotonic() + 2):
            response = await dispatcher.dispatch(
                "busy", "sessions.messages.hydrate", {"key": key}, case.context,
            )
        assert not response.ok
        assert response.error.code == "STORAGE_BUSY"
        assert response.error.retryable
        assert response.error.retry_after_ms == 100
        assert response.error.details["operation"] == "list_recent_agent_tasks"
        assert response.error.details["stage"] == "permit"
        assert response.error.details["resource"] == "session_storage_recovery_read_pool"
    with recovery_read_scope(key, deadline=time.monotonic() + 2):
        response = await dispatcher.dispatch(
            "recovered", "sessions.messages.hydrate", {"key": key}, case.context,
        )
    assert response.ok and response.payload["last_task"]["task_id"] == case.record.task_id


@pytest.mark.parametrize("source", ["recent", "runtime", "storage", "batch"])
async def test_each_task_reader_preserves_typed_busy_without_fallback(source):
    busy = StorageBusyError("original-operation", waited_ms=17, retry_after_ms=31,
                            stage="deadline", resource="original-resource")
    failed = AsyncMock(side_effect=busy)
    if source == "recent":
        storage = SimpleNamespace(list_recent_agent_tasks=failed)
        context = SimpleNamespace(task_runtime=SimpleNamespace(list=AsyncMock()))
    elif source == "runtime":
        storage = None
        context = SimpleNamespace(task_runtime=SimpleNamespace(list=failed))
    elif source == "storage":
        storage = SimpleNamespace(list_agent_tasks=failed)
        context = SimpleNamespace(task_runtime=None)
    else:
        storage = SimpleNamespace(list_agent_tasks_for_sessions=failed)
        context = SimpleNamespace(task_runtime=SimpleNamespace(list=AsyncMock()))
    with pytest.raises(StorageBusyError) as caught:
        if source == "batch":
            await rpc_sessions._list_task_rows_by_session(
                context, storage, ["agent:main:webchat:read"],
            )
        else:
            await rpc_sessions._list_task_rows(context, storage, "agent:main:webchat:read")
    assert caught.value is busy
    failed.assert_awaited_once()


async def test_legacy_deadline_is_typed_busy_instead_of_empty_success():
    storage = SimpleNamespace(
        list_recent_agent_tasks=AsyncMock(side_effect=TimeoutError("private text")),
    )
    with pytest.raises(StorageBusyError) as caught:
        await rpc_sessions._list_task_rows(SimpleNamespace(task_runtime=None), storage,
                                          "agent:main:webchat:read")
    assert caught.value.stage == "deadline"
    assert caught.value.resource == "session_task_state"
    assert isinstance(caught.value.__cause__, TimeoutError)


async def test_legacy_fallback_warning_contains_type_without_exception_text(monkeypatch):
    warnings = []
    monkeypatch.setattr(rpc_sessions, "log", SimpleNamespace(
        warning=lambda event, **fields: warnings.append({"event": event, **fields})))
    expected = SimpleNamespace(task_id="legacy-task")
    storage = SimpleNamespace(
        list_recent_agent_tasks=AsyncMock(side_effect=NotImplementedError("SECRET chat body")),
        list_agent_tasks=AsyncMock(return_value=[expected]),
    )
    rows = await rpc_sessions._list_task_rows(SimpleNamespace(task_runtime=None), storage,
                                            "agent:main:webchat:read")
    assert rows == [expected]
    assert warnings == [{"event": "sessions.recent_agent_task_storage_state_failed",
                         "session_key": "agent:main:webchat:read",
                         "exception_type": "NotImplementedError"}]


async def test_busy_warning_contains_only_safe_read_diagnostics(monkeypatch):
    warnings = []
    monkeypatch.setattr(rpc_sessions, "log", SimpleNamespace(
        warning=lambda event, **fields: warnings.append({"event": event, **fields})))
    busy = StorageBusyError("list_recent_agent_tasks", waited_ms=0, retry_after_ms=100,
                            stage="permit", resource="session_storage_recovery_read_pool")
    storage = SimpleNamespace(list_recent_agent_tasks=AsyncMock(side_effect=busy))
    with pytest.raises(StorageBusyError):
        await rpc_sessions._list_task_rows(SimpleNamespace(task_runtime=None), storage,
                                          "agent:main:webchat:read")
    assert warnings == [{"event": "sessions.recent_agent_task_storage_state_failed",
        "session_key": "agent:main:webchat:read", "exception_type": "StorageBusyError",
        "operation": "list_recent_agent_tasks", "stage": "permit",
        "resource": "session_storage_recovery_read_pool", "waited_ms": 0,
        "retry_after_ms": 100}]


async def test_task_read_cancellation_is_not_logged_or_retried(monkeypatch):
    warnings = []
    monkeypatch.setattr(rpc_sessions, "log", SimpleNamespace(
        warning=lambda event, **fields: warnings.append({"event": event, **fields})))
    fallback = AsyncMock()
    storage = SimpleNamespace(
        list_recent_agent_tasks=AsyncMock(side_effect=asyncio.CancelledError),
        list_agent_tasks=fallback,
    )
    with pytest.raises(asyncio.CancelledError):
        await rpc_sessions._list_task_rows(SimpleNamespace(task_runtime=None), storage,
                                          "agent:main:webchat:read")
    assert not warnings
    fallback.assert_not_awaited()


async def test_genuinely_empty_ledger_remains_authoritative_success(task_state_case, monkeypatch):
    case = task_state_case
    key = "agent:main:webchat:never-had-a-task"
    fallback = AsyncMock(side_effect=AssertionError("An empty successful read is authoritative"))
    monkeypatch.setattr(case.runtime, "list", fallback)
    with recovery_read_scope(key, deadline=time.monotonic() + 2):
        rows = await rpc_sessions._list_task_rows(case.context, case.storage, key)
    assert rows == []
    fallback.assert_not_awaited()
