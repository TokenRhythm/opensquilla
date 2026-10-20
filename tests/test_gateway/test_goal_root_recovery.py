"""Goal rejection, ownership and bounded durable-owner reconciliation contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import opensquilla.gateway.goal_service as goal_module
from opensquilla.gateway.config import GoalConfig
from opensquilla.gateway.goal_service import GoalService
from opensquilla.gateway.rpc import RpcHandlerError, RpcRegistry
from opensquilla.gateway.rpc_goals import (
    _handle_goals_pause,
    _handle_goals_reattach,
    _handle_goals_resume,
    _handle_goals_set,
    _translate_goal_errors,
)
from opensquilla.gateway.session_lifecycle import TaskLifecycleEvent
from opensquilla.session.goals import ClaimCurrentGoalMutation, GoalConflictError
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionNode,
    TranscriptEntry,
)
from opensquilla.session.storage import SessionStorage, StorageBusyError
from tests.test_gateway.test_goal_rpc import (
    SOURCE_KEY,
    _mutation_params,
    _open_goal_rpc_stack,
    _reattach_params,
    _set_params,
    _settle_set_task,
    _wait_for_goal,
)
from tests.test_session.test_goal_storage import (
    SESSION_ID,
    SESSION_KEY,
    _command,
    _expected,
    _set_goal,
)


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[SessionStorage]:
    value = SessionStorage(str(tmp_path / "goal-roots.sqlite"))
    await value.connect()
    await value.upsert_session(SessionNode(
        session_key=SESSION_KEY, session_id=SESSION_ID, agent_id="main",
        created_at=100, updated_at=100, epoch=0,
    ))
    try:
        yield value
    finally:
        await value.close()


def _service(storage: SessionStorage) -> GoalService:
    async def emit(*_args: Any) -> None:
        pass

    return GoalService(
        storage=storage, session_manager=None, task_runtime=SimpleNamespace(),
        event_emitter=emit, subscription_manager=SimpleNamespace(),
        config=GoalConfig(execution_enabled=True),
    )


def _terminal(task_id: str = "task-1", *, persisted: bool = True) -> TaskLifecycleEvent:
    return TaskLifecycleEvent(
        phase="terminal", session_key=SESSION_KEY, task_id=task_id,
        task_status=AgentTaskStatus.SUCCEEDED, run_kind="goal",
        terminal_persisted=persisted,
    )


async def _drain(service: GoalService, key: str = SESSION_KEY) -> None:
    task = service._kick_tasks.get(key)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=5)


async def _accept_manual_goal_task(storage: SessionStorage) -> TaskLifecycleEvent:
    accepted = await storage.accept_turn(
        TranscriptEntry(
            session_id=SESSION_ID, session_key=SESSION_KEY, message_id="manual-message",
            role="user", content="Explicit manual input", created_at=300,
        ),
        expected_epoch=0, updated_at=300,
        task_record=AgentTaskRecord(
            task_id="manual-task", session_key=SESSION_KEY,
            status=AgentTaskStatus.QUEUED, created_at=300, updated_at=300,
        ),
        source_scope="web:webchat:operator", request_session_key=SESSION_KEY,
        client_request_id="manual-request",
        request_fingerprint=hashlib.sha256(b"explicit-manual-input").hexdigest(),
        goal_mutation=ClaimCurrentGoalMutation(),
    )
    assert accepted.goal_context is not None and accepted.goal_context.automatic is False
    await storage.update_agent_task(
        "manual-task", status=AgentTaskStatus.SUCCEEDED, started_at=310, finished_at=350,
    )
    return _terminal("manual-task")


@pytest.mark.parametrize("code,retryable", [("STALE_GOAL", True), ("GOAL_BUSY", False)])
@pytest.mark.parametrize("busy", [False, True])
async def test_known_rejection_survives_optional_snapshot_failure_on_wire(
    tmp_path: Path, code: str, retryable: bool, busy: bool,
) -> None:
    async with _open_goal_rpc_stack(tmp_path / "wire.sqlite") as stack:
        async def reject() -> dict[str, Any]:
            raise GoalConflictError(code, "Known command rejection", current=object())

        async def snapshot(_goal: Any) -> None:
            if busy:
                raise StorageBusyError("snapshot", waited_ms=1, retry_after_ms=200)
            raise OSError("Optional snapshot failed")

        async def handler(_params: Any, _ctx: Any) -> dict[str, Any]:
            return await _translate_goal_errors(
                reject(), service=SimpleNamespace(snapshot=snapshot),
            )

        registry = RpcRegistry()
        registry.register("goals.status", handler, "operator.read")
        response = await registry.dispatch("known-rejection", "goals.status", {}, stack.context)
        assert response.ok is False and response.error is not None
        assert response.error.code == code
        assert response.error.accepted is False
        assert response.error.retryable is retryable
        assert response.error.details is None


@pytest.mark.parametrize("retryable", [False, True])
async def test_authority_contract_distinguishes_denial_from_temporary_unavailability(
    retryable: bool,
) -> None:
    async def reject() -> dict[str, Any]:
        raise GoalConflictError(
            "GOAL_AUTHORITY_UNAVAILABLE", "Authority failed", retryable=retryable,
        )

    with pytest.raises(RpcHandlerError) as error:
        await _translate_goal_errors(reject())
    assert error.value.accepted is False
    assert error.value.retryable is retryable


@pytest.mark.parametrize("reason", ["activation_failed", "process_restart"])
@pytest.mark.parametrize("status,started_at", [
    (AgentTaskStatus.RUNNING, None),
    (AgentTaskStatus.QUEUED, 210),
    (AgentTaskStatus.SUCCEEDED, 210),
])
async def test_late_activation_cleanup_cannot_reclaim_started_task(
    storage: SessionStorage, reason: str, status: AgentTaskStatus, started_at: int | None,
) -> None:
    accepted = await _set_goal(storage)
    await storage.update_agent_task("task-1", status=status, started_at=started_at)
    before_task = await storage.get_agent_task("task-1")
    before_goal = await storage.get_goal(SESSION_KEY)
    assert accepted.goal_context is not None
    assert await storage.compensate_goal_activation_failure(
        accepted.goal_context, reason=reason,
    ) is None
    assert await storage.get_agent_task("task-1") == before_task
    assert await storage.get_goal(SESSION_KEY) == before_goal


@pytest.mark.parametrize("successor", [False, True])
async def test_activation_cleanup_releases_deleted_goals_task_but_preserves_successor(
    storage: SessionStorage, successor: bool,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    task = await storage.get_agent_task("task-1")
    assert task is not None
    await storage.update_agent_task("task-1", details={
        **(task.details or {}),
        "admission_owner": {"resident_lease": "held", "compute_lease": "not_acquired"},
    })
    await storage.clear_goal(
        session_key=SESSION_KEY, expected=_expected(accepted.goal), command=_command("clear"),
    )
    service = _service(storage)
    try:
        if successor:
            # A replacement can be accepted once earlier cleanup released the
            # old queued task; a delayed duplicate cleanup must preserve it.
            await storage.compensate_goal_activation_failure(accepted.goal_context)
            await _set_goal(
                storage, goal_id="next-goal", task_id="next-task", message_id="next-msg",
            )
            # Cleanup for the previous task must not revoke replacement authority.
            lease = SimpleNamespace(goal_id="next-goal", session_id=SESSION_ID, epoch=0)
            service._leases[SESSION_KEY] = lease
        before_goal = await storage.get_goal(SESSION_KEY)
        assert await service.compensate_activation_failure(
            accepted.goal_context.as_task_detail(),
        ) is None
        cleaned = await storage.get_agent_task("task-1")
        assert cleaned is not None and cleaned.status == AgentTaskStatus.ABANDONED
        assert cleaned.details["admission_owner"]["resident_lease"] == "released"
        assert await storage.get_goal(SESSION_KEY) == before_goal
        if successor:
            assert service._leases[SESSION_KEY] is lease
            assert (await storage.get_agent_task("next-task")).status == AgentTaskStatus.QUEUED
    finally:
        service._leases.clear()
        await service.close()


@pytest.mark.parametrize("status", ["active", "paused", "complete"])
async def test_idle_reconciles_durable_terminal_owner_once_in_every_goal_state(
    storage: SessionStorage, status: str,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal is not None and accepted.goal_context is not None
    if status == "complete":
        await storage.commit_goal_terminal(accepted.goal_context, status="complete")
    elif status == "paused":
        await storage.pause_goal(
            session_key=SESSION_KEY, expected=_expected(accepted.goal), command=_command("pause"),
            reason="user",
        )
    await storage.update_agent_task(
        "task-1", status=AgentTaskStatus.SUCCEEDED, started_at=210, finished_at=250,
    )
    service = _service(storage)
    try:
        await service._kick_if_idle(SESSION_KEY)
        goal = await storage.get_goal(SESSION_KEY)
        assert goal is not None and goal.active_task_id is None
        assert goal.status == status and goal.turns_settled == 1 and goal.active_time_ms == 40
        await service._kick_if_idle(SESSION_KEY)
        assert await storage.get_goal(SESSION_KEY) == goal
    finally:
        await service.close()


@pytest.mark.parametrize("settlement_fault", [False, True])
async def test_real_sqlite_write_lock_recovers_terminal_owner_without_client_wake(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch, settlement_fault: bool,
) -> None:
    accepted = await _set_goal(storage)
    assert accepted.goal_context is not None
    await storage.commit_goal_terminal(accepted.goal_context, status="complete")
    await storage.update_agent_task(
        "task-1", status=AgentTaskStatus.SUCCEEDED, started_at=210, finished_at=250,
    )
    storage._busy_budget_seconds = 0.025
    service = _service(storage)
    attempts: list[str] = []
    settle = storage.settle_goal_task
    compensate = storage.compensate_terminal_persistence_failure

    async def observed_settle(*args: Any, **kwargs: Any) -> Any:
        attempts.append("settle")
        if settlement_fault and attempts == ["settle"]:
            raise OSError("Synthetic permanent settlement failure before locked fallback")
        return await settle(*args, **kwargs)

    async def observed_compensate(*args: Any, **kwargs: Any) -> Any:
        attempts.append("compensate")
        return await compensate(*args, **kwargs)

    monkeypatch.setattr(storage, "settle_goal_task", observed_settle)
    monkeypatch.setattr(storage, "compensate_terminal_persistence_failure", observed_compensate)
    lock = sqlite3.connect(storage._db_path, timeout=0)
    lock.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StorageBusyError):
            await service.on_task_lifecycle(_terminal())
        goal = await storage.get_goal(SESSION_KEY)
        assert goal is not None and goal.active_task_id == "task-1" and goal.turns_settled == 0
        assert attempts == (["settle", "compensate"] if settlement_fault else ["settle"])
        lock.rollback()
        # The retained server runner owns the next attempt; no browser or new idle wake.
        await _drain(service)
        goal = await storage.get_goal(SESSION_KEY)
        assert goal is not None and goal.status == "complete" and goal.active_task_id is None
        assert goal.turns_settled == 1 and goal.active_time_ms == 40
        assert (await storage.get_agent_task("task-1")).status == AgentTaskStatus.SUCCEEDED
        assert SESSION_KEY not in service._idle_retries
    finally:
        lock.rollback()
        lock.close()
        await service.close()


async def test_long_retry_after_and_duplicate_wakes_never_shorten_floor_or_extend_episode(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(storage)
    monkeypatch.setattr(goal_module, "_IDLE_RETRY_WINDOW_SECONDS", 0.03)
    calls = 0

    async def blocked(_key: str) -> None:
        nonlocal calls
        calls += 1
        raise StorageBusyError("test", waited_ms=1, retry_after_ms=300_000)

    monkeypatch.setattr(service, "_kick_if_idle", blocked)
    try:
        service.schedule_idle_evaluation(SESSION_KEY)
        await asyncio.sleep(0)
        retry = service._idle_retries[SESSION_KEY]
        deadline, floor = retry.deadline, retry.not_before
        for _ in range(20):
            service.schedule_idle_evaluation(SESSION_KEY)
            await service.on_task_lifecycle(_terminal())
        await _drain(service)
        assert calls == 1 and retry.exhausted
        assert retry.deadline == deadline and retry.not_before == floor
        assert floor > time.monotonic() + 290
        for _ in range(20):
            service.schedule_idle_evaluation(SESSION_KEY)
        await _drain(service)
        assert calls == 1
        service._reset_idle_retry(SESSION_KEY)
        assert service._idle_retries[SESSION_KEY].not_before == floor
        service.schedule_idle_evaluation(SESSION_KEY)
        await _drain(service)
        assert calls == 1  # Even explicit recovery cannot bypass Retry-After.
    finally:
        await service.close()


async def test_retry_attempt_limit_survives_repeated_external_wakes(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(storage)
    monkeypatch.setattr(goal_module, "_IDLE_RETRY_BASE_SECONDS", 0.001)
    calls = 0

    async def blocked(_key: str) -> None:
        nonlocal calls
        calls += 1
        raise StorageBusyError("test", waited_ms=1, retry_after_ms=0)

    monkeypatch.setattr(service, "_kick_if_idle", blocked)
    try:
        service.schedule_idle_evaluation(SESSION_KEY)
        await _drain(service)
        assert calls == goal_module._IDLE_RETRY_LIMIT
        for _ in range(15):
            service.schedule_idle_evaluation(SESSION_KEY)
            await _drain(service)
        assert calls == goal_module._IDLE_RETRY_LIMIT
    finally:
        await service.close()


@pytest.mark.parametrize("settlement_lock", ["none", "recover", "exhaust"])
async def test_new_manual_terminal_settles_without_reopening_exhausted_continuation(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch, settlement_lock: str,
) -> None:
    created = await _set_goal(storage)
    assert created.goal_context is not None
    await storage.update_agent_task("task-1", status=AgentTaskStatus.SUCCEEDED)
    await storage.settle_goal_task(created.goal_context, max_turns=50, runtime_budget_seconds=3600)
    service = _service(storage)
    kick = service._kick_if_idle
    continuation_calls = 0

    async def blocked_continuation(_key: str) -> None:
        nonlocal continuation_calls
        continuation_calls += 1
        raise StorageBusyError("continuation", waited_ms=1, retry_after_ms=1)

    monkeypatch.setattr(goal_module, "_IDLE_RETRY_BASE_SECONDS", 0.001)
    monkeypatch.setattr(goal_module.log, "warning", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_kick_if_idle", blocked_continuation)
    lock = sqlite3.connect(storage._db_path, timeout=0)
    failed = asyncio.Event()
    attempts: list[float] = []
    settle = storage.settle_goal_task

    async def observed_settle(*args: Any, **kwargs: Any) -> Any:
        attempts.append(time.monotonic())
        try:
            return await settle(*args, **kwargs)
        except StorageBusyError:
            failed.set()
            raise

    try:
        service.schedule_idle_evaluation(SESSION_KEY)
        await _drain(service)
        original = service._idle_retries[SESSION_KEY]
        assert original.exhausted and continuation_calls == goal_module._IDLE_RETRY_LIMIT
        original_budget = (original.deadline, original.failures)
        original_floor = original.not_before
        monkeypatch.setattr(service, "_kick_if_idle", kick)
        event = await _accept_manual_goal_task(storage)
        storage._busy_budget_seconds = 0.005
        monkeypatch.setattr(storage, "settle_goal_task", observed_settle)
        if settlement_lock != "none":
            lock.execute("BEGIN IMMEDIATE")
        await service.on_task_lifecycle(event)
        retry = service._idle_retries[SESSION_KEY]
        assert retry is not original and retry.continuation_retry is original
        assert retry.terminal_task_id == "manual-task"
        deadline = retry.deadline
        if settlement_lock != "none":
            await asyncio.wait_for(failed.wait(), timeout=2)
        for _ in range(20):
            await service.on_task_lifecycle(event)
            service.schedule_idle_evaluation(SESSION_KEY)
        if settlement_lock == "recover":
            lock.rollback()
        await _drain(service)
        goal = await storage.get_goal(SESSION_KEY)
        assert goal is not None
        if settlement_lock == "exhaust":
            assert retry.exhausted and retry.failures == goal_module._IDLE_RETRY_LIMIT
            assert len(attempts) == goal_module._IDLE_RETRY_LIMIT
            assert goal.active_task_id == "manual-task" and goal.turns_settled == 1
            assert service._idle_retries[SESSION_KEY] is retry
        else:
            assert goal.active_task_id is None and goal.turns_started == goal.turns_settled == 2
            assert goal.active_time_ms == 40
            assert len(attempts) == (2 if settlement_lock == "recover" else 1)
            assert service._idle_retries[SESSION_KEY] is original
        assert attempts[0] >= original_floor
        count = len(attempts)
        for _ in range(20):
            await service.on_task_lifecycle(event)
            service.schedule_idle_evaluation(SESSION_KEY)
        await _drain(service)
        assert len(attempts) == count and retry.deadline == deadline
        assert continuation_calls == goal_module._IDLE_RETRY_LIMIT
        assert original.exhausted and (original.deadline, original.failures) == original_budget
    finally:
        lock.rollback()
        lock.close()
        await service.close()


async def test_new_terminal_waits_for_old_retry_after_without_duplicate_budget_reset(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await _set_goal(storage)
    assert created.goal_context is not None
    await storage.update_agent_task("task-1", status=AgentTaskStatus.SUCCEEDED)
    await storage.settle_goal_task(created.goal_context, max_turns=50, runtime_budget_seconds=3600)
    service = _service(storage)
    kick = service._kick_if_idle
    monkeypatch.setattr(goal_module, "_IDLE_RETRY_WINDOW_SECONDS", 0.03)
    continuation_calls = 0

    async def blocked_continuation(_key: str) -> None:
        nonlocal continuation_calls
        continuation_calls += 1
        raise StorageBusyError("continuation", waited_ms=1, retry_after_ms=500)

    monkeypatch.setattr(service, "_kick_if_idle", blocked_continuation)
    monkeypatch.setattr(goal_module.log, "warning", lambda *_args, **_kwargs: None)
    attempts: list[float] = []
    settle = storage.settle_goal_task

    async def observed_settle(*args: Any, **kwargs: Any) -> Any:
        attempts.append(time.monotonic())
        return await settle(*args, **kwargs)

    try:
        service.schedule_idle_evaluation(SESSION_KEY)
        await _drain(service)
        original = service._idle_retries[SESSION_KEY]
        assert original.exhausted and original.not_before > time.monotonic()
        monkeypatch.setattr(goal_module, "_IDLE_RETRY_WINDOW_SECONDS", 2.0)
        monkeypatch.setattr(service, "_kick_if_idle", kick)
        event = await _accept_manual_goal_task(storage)
        monkeypatch.setattr(storage, "settle_goal_task", observed_settle)
        await service.on_task_lifecycle(event)
        retry = service._idle_retries[SESSION_KEY]
        deadline, floor = retry.deadline, retry.not_before
        assert floor == original.not_before
        for _ in range(20):
            await service.on_task_lifecycle(event)
            service.schedule_idle_evaluation(SESSION_KEY)
        assert not attempts
        assert service._idle_retries[SESSION_KEY] is retry
        assert (retry.deadline, retry.not_before) == (deadline, floor)
        await _drain(service)
        goal = await storage.get_goal(SESSION_KEY)
        assert goal is not None and goal.active_task_id is None
        assert goal.turns_started == goal.turns_settled == 2
        assert len(attempts) == 1 and attempts[0] >= floor
        assert service._idle_retries[SESSION_KEY] is original
        assert original.exhausted and continuation_calls == 1
    finally:
        await service.close()


@pytest.mark.parametrize("previous_episode", ["continuation", "settlement"])
@pytest.mark.parametrize("qualification_read", ["get_goal", "get_agent_task"])
@pytest.mark.parametrize("read_failures", [1, 4])
async def test_new_terminal_qualification_read_failure_keeps_a_bounded_obligation(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
    previous_episode: str, qualification_read: str, read_failures: int,
) -> None:
    created = await _set_goal(storage)
    assert created.goal_context is not None
    await storage.update_agent_task("task-1", status=AgentTaskStatus.SUCCEEDED)
    if previous_episode == "continuation":
        await storage.settle_goal_task(
            created.goal_context, max_turns=50, runtime_budget_seconds=3600,
        )
    service = _service(storage)
    kick = service._kick_if_idle
    continuation_calls = 0

    async def blocked_continuation(_key: str) -> None:
        nonlocal continuation_calls
        continuation_calls += 1
        raise StorageBusyError("continuation", waited_ms=1, retry_after_ms=0)

    monkeypatch.setattr(goal_module, "_IDLE_RETRY_BASE_SECONDS", 0.001)
    monkeypatch.setattr(goal_module.log, "warning", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_kick_if_idle", blocked_continuation)
    lock = sqlite3.connect(storage._db_path, timeout=0)
    try:
        service.schedule_idle_evaluation(SESSION_KEY)
        await _drain(service)
        continuation = service._idle_retries[SESSION_KEY]
        monkeypatch.setattr(service, "_kick_if_idle", kick)
        if previous_episode == "settlement":
            storage._busy_budget_seconds = 0.005
            lock.execute("BEGIN IMMEDIATE")
            await service.on_task_lifecycle(_terminal())
            await _drain(service)
            lock.rollback()
            prior = service._idle_retries[SESSION_KEY]
            assert prior.continuation_retry is continuation and prior.exhausted
            # A competing durable settlement releases A while this service still
            # retains A's exhausted budget; B can then genuinely claim the Goal.
            await storage.settle_goal_task(
                created.goal_context, max_turns=50, runtime_budget_seconds=3600,
            )
        prior = service._idle_retries[SESSION_KEY]
        prior_budget = (prior.deadline, prior.failures, prior.exhausted)
        event = await _accept_manual_goal_task(storage)
        read = getattr(storage, qualification_read)
        get_goal = storage.get_goal
        reads = 0

        async def fail_first_read(*args: Any, **kwargs: Any) -> Any:
            nonlocal reads
            reads += 1
            if reads <= read_failures:
                # Inject a service read outage. A WAL writer lock alone does not
                # normally block this read, so do not label this a lock effect.
                raise StorageBusyError("qualification", waited_ms=1, retry_after_ms=60)
            return await read(*args, **kwargs)

        monkeypatch.setattr(storage, qualification_read, fail_first_read)
        with pytest.raises(StorageBusyError):
            await service.on_task_lifecycle(event)
        assert service._idle_retries[SESSION_KEY] is prior
        pending = prior.pending_settlement
        assert pending is not None and pending.terminal_task_id == "manual-task"
        assert pending.failures == 1 and pending.continuation_retry is None
        deadline, floor = pending.deadline, pending.not_before
        for _ in range(20):
            await service.on_task_lifecycle(event)
            await service.on_task_lifecycle(_terminal())
            service.schedule_idle_evaluation(SESSION_KEY)
        assert reads == 1
        assert (pending.deadline, pending.not_before) == (deadline, floor)
        await _drain(service)
        goal = await get_goal(SESSION_KEY)
        assert goal is not None
        if read_failures == goal_module._IDLE_RETRY_LIMIT:
            assert reads == read_failures and pending.exhausted
            assert goal.active_task_id == "manual-task" and goal.turns_settled == 1
            for _ in range(20):
                await service.on_task_lifecycle(event)
                service.schedule_idle_evaluation(SESSION_KEY)
            await _drain(service)
            assert reads == read_failures and pending.deadline == deadline
            assert service._idle_retries[SESSION_KEY] is prior
            assert prior.pending_settlement is pending
            service._reset_idle_retry(SESSION_KEY)
            assert service._idle_retries[SESSION_KEY].not_before >= pending.not_before
        else:
            assert goal.active_task_id is None and goal.turns_started == goal.turns_settled == 2
            assert goal.active_time_ms == 40
            assert service._idle_retries[SESSION_KEY] is continuation
            assert prior.pending_settlement is None
        assert (prior.deadline, prior.failures, prior.exhausted) == prior_budget
        assert continuation.exhausted and continuation_calls == goal_module._IDLE_RETRY_LIMIT
    finally:
        lock.rollback()
        lock.close()
        await service.close()


async def test_stale_terminal_candidate_cannot_reopen_current_owners_exhausted_settlement(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _set_goal(storage)
    await storage.update_agent_task("task-1", status=AgentTaskStatus.SUCCEEDED)
    service = _service(storage)
    monkeypatch.setattr(goal_module, "_IDLE_RETRY_BASE_SECONDS", 0.001)
    monkeypatch.setattr(goal_module.log, "warning", lambda *_args, **_kwargs: None)
    storage._busy_budget_seconds = 0.005
    lock = sqlite3.connect(storage._db_path, timeout=0)
    try:
        lock.execute("BEGIN IMMEDIATE")
        with pytest.raises(StorageBusyError):
            await service.on_task_lifecycle(_terminal())
        await _drain(service)
        lock.rollback()
        original = service._idle_retries[SESSION_KEY]
        assert original.exhausted and original.terminal_task_id == "task-1"
        budget = (original.deadline, original.failures)
        get_goal = storage.get_goal
        reads = 0

        async def fail_first_read(key: str) -> Any:
            nonlocal reads
            reads += 1
            if reads == 1:
                raise StorageBusyError("qualification", waited_ms=1, retry_after_ms=60)
            return await get_goal(key)

        monkeypatch.setattr(storage, "get_goal", fail_first_read)
        with pytest.raises(StorageBusyError):
            await service.on_task_lifecycle(_terminal("stale-task"))
        pending = original.pending_settlement
        assert pending is not None
        for _ in range(20):
            await service.on_task_lifecycle(_terminal("stale-task"))
            await service.on_task_lifecycle(_terminal())
            service.schedule_idle_evaluation(SESSION_KEY)
        await _drain(service)
        goal = await storage.get_goal(SESSION_KEY)
        assert goal is not None and goal.active_task_id == "task-1"
        assert goal.turns_settled == 0
        assert service._idle_retries[SESSION_KEY] is original
        assert original.pending_settlement is None
        assert original.exhausted and (original.deadline, original.failures) == budget
    finally:
        lock.rollback()
        lock.close()
        await service.close()


async def test_exhausted_stale_candidate_does_not_exhaust_current_owner(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _set_goal(storage)
    await storage.update_agent_task("task-1", status=AgentTaskStatus.SUCCEEDED)
    service = _service(storage)
    monkeypatch.setattr(goal_module, "_IDLE_RETRY_BASE_SECONDS", 0.001)
    monkeypatch.setattr(goal_module.log, "warning", lambda *_args, **_kwargs: None)
    storage._busy_budget_seconds = 0.005
    lock = sqlite3.connect(storage._db_path, timeout=0)
    try:
        lock.execute("BEGIN IMMEDIATE")
        with pytest.raises(StorageBusyError):
            await service.on_task_lifecycle(_terminal())
        lock.rollback()
        original = service._idle_retries[SESSION_KEY]
        assert original.failures == 1 and not original.exhausted
        get_goal = storage.get_goal
        qualification_failures = 0

        async def fail_candidate_reads(key: str) -> Any:
            nonlocal qualification_failures
            if qualification_failures < goal_module._IDLE_RETRY_LIMIT:
                qualification_failures += 1
                raise StorageBusyError("qualification", waited_ms=1, retry_after_ms=1)
            return await get_goal(key)

        monkeypatch.setattr(storage, "get_goal", fail_candidate_reads)
        with pytest.raises(StorageBusyError):
            await service.on_task_lifecycle(_terminal("stale-task"))
        pending = original.pending_settlement
        assert pending is not None
        for _ in range(20):
            await service.on_task_lifecycle(_terminal("stale-task"))
            await service.on_task_lifecycle(_terminal())
            service.schedule_idle_evaluation(SESSION_KEY)
        await _drain(service)
        goal = await storage.get_goal(SESSION_KEY)
        assert goal is not None and goal.active_task_id is None
        assert goal.turns_started == goal.turns_settled == 1
        assert qualification_failures == pending.failures == goal_module._IDLE_RETRY_LIMIT
        assert pending.exhausted and original.failures == 1
        assert not original.exhausted
    finally:
        lock.rollback()
        lock.close()
        await service.close()


async def test_unknown_terminal_persistence_never_reclaims_a_durable_running_task(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _set_goal(storage)
    await storage.update_agent_task("task-1", status=AgentTaskStatus.RUNNING, started_at=210)
    before = await storage.get_agent_task("task-1")
    service = _service(storage)

    async def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise StorageBusyError("terminal-persistence", waited_ms=1, retry_after_ms=0)

    monkeypatch.setattr(storage, "compensate_terminal_persistence_failure", unavailable)
    monkeypatch.setattr(goal_module, "_IDLE_RETRY_BASE_SECONDS", 0.001)
    try:
        with pytest.raises(StorageBusyError):
            await service.on_task_lifecycle(_terminal(persisted=False))
        await _drain(service)
        assert await storage.get_agent_task("task-1") == before
        assert (await storage.get_goal(SESSION_KEY)).active_task_id == "task-1"
    finally:
        await service.close()


async def test_transient_settlement_recovery_launches_exactly_one_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[str] = []
    service: GoalService | None = None

    async def handler(run: Any) -> None:
        runs.append(run.task_id)
        if len(runs) == 2:
            assert service is not None
            await service.commit_model_status(run.goal_context, status="complete", reason=None)

    async with _open_goal_rpc_stack(
        tmp_path / "continuation.sqlite", handler=handler, wire_lifecycle=True,
    ) as stack:
        service = stack.service
        settle = stack.storage.settle_goal_task
        first = True

        async def transient(*args: Any, **kwargs: Any) -> Any:
            nonlocal first
            if first:
                first = False
                raise StorageBusyError("settlement", waited_ms=1, retry_after_ms=25)
            return await settle(*args, **kwargs)

        monkeypatch.setattr(stack.storage, "settle_goal_task", transient)
        monkeypatch.setattr(goal_module, "_IDLE_RETRY_BASE_SECONDS", 0.01)
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=5)
        complete = await _wait_for_goal(
            stack.storage, lambda goal: goal.status == "complete" and goal.active_task_id is None,
        )
        await _drain(service, SOURCE_KEY)
        for _ in range(10):
            service.schedule_idle_evaluation(SOURCE_KEY)
        await _drain(service, SOURCE_KEY)
        assert len(runs) == len(set(runs)) == 2
        assert complete.turns_started == complete.turns_settled == 2
        assert len(await stack.storage.list_agent_tasks(session_key=SOURCE_KEY, limit=20)) == 2


async def test_existing_terminal_writer_wakes_goal_reconciliation_after_persistence_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service: GoalService | None = None
    compensation_failed = asyncio.Event()
    unavailable = True
    runs = 0
    started = asyncio.Event()
    finish = asyncio.Event()

    async def handler(run: Any) -> None:
        nonlocal runs
        runs += 1
        assert service is not None
        await service.commit_model_status(run.goal_context, status="complete", reason=None)
        started.set()
        await finish.wait()

    async with _open_goal_rpc_stack(
        tmp_path / "terminal-writer.sqlite", handler=handler, wire_lifecycle=True,
    ) as stack:
        service = stack.service
        persist = stack.storage.settle_agent_task
        compensate = stack.storage.compensate_terminal_persistence_failure

        async def faulted_persist(*args: Any, **kwargs: Any) -> Any:
            if unavailable:
                raise StorageBusyError("terminal-write", waited_ms=1, retry_after_ms=0)
            return await persist(*args, **kwargs)

        async def faulted_compensate(*args: Any, **kwargs: Any) -> Any:
            if unavailable:
                compensation_failed.set()
                raise StorageBusyError("goal-compensation", waited_ms=1, retry_after_ms=0)
            return await compensate(*args, **kwargs)

        monkeypatch.setattr(stack.storage, "settle_agent_task", faulted_persist)
        monkeypatch.setattr(
            stack.storage, "compensate_terminal_persistence_failure", faulted_compensate,
        )
        monkeypatch.setattr(goal_module, "_IDLE_RETRY_BASE_SECONDS", 0.001)
        created = await _handle_goals_set(_set_params(), stack.context)
        await asyncio.wait_for(started.wait(), timeout=5)
        driver = stack.runtime._tasks[created["taskId"]].asyncio_task
        finish.set()
        await asyncio.wait_for(compensation_failed.wait(), timeout=5)
        await asyncio.wait_for(asyncio.shield(driver), timeout=5)

        async def idle_runner_finished() -> None:
            # Runtime.wait can expose its terminal fallback before the driver
            # publishes the final idle wake. Drain that wake too, so the later
            # persistence retry is the only remaining source of reconciliation.
            while True:
                await _drain(service, SOURCE_KEY)
                await asyncio.sleep(0.01)
                if (
                    not await stack.runtime.has_session_work(SOURCE_KEY)
                    and SOURCE_KEY not in service._kick_tasks
                ):
                    return

        await asyncio.wait_for(idle_runner_finished(), timeout=5)
        task = await stack.storage.get_agent_task(created["taskId"])
        assert task is not None and task.status == AgentTaskStatus.RUNNING
        assert (await stack.storage.get_goal(SOURCE_KEY)).active_task_id == task.task_id
        await _drain(service, SOURCE_KEY)
        assert SOURCE_KEY not in service._kick_tasks

        # Existing TaskRuntime retry owns the retained terminal fact. Its
        # successful write must wake Goal reconciliation without client input.
        unavailable = False
        complete = await _wait_for_goal(
            stack.storage, lambda goal: goal.status == "complete" and goal.active_task_id is None,
        )
        await _drain(service, SOURCE_KEY)
        assert complete.turns_started == complete.turns_settled == runs == 1
        recovered = await stack.storage.get_agent_task(task.task_id)
        assert recovered is not None and recovered.status == AgentTaskStatus.SUCCEEDED


@pytest.mark.parametrize("guard_reason", ["empty_continuations", "no_progress"])
async def test_guard_pause_and_owner_release_rollback_and_recover_in_one_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard_reason: str,
) -> None:
    runs: list[str] = []
    failed = asyncio.Event()
    armed = False
    service: GoalService | None = None

    async def handler(run: Any) -> None:
        runs.append(run.task_id)
        task = await stack.storage.get_agent_task(run.task_id)
        assert task is not None
        details = dict(task.details or {})
        details["activity_snapshot"] = {
            "version": 2, "task_id": run.task_id, "turn_id": run.task_id,
            "complete": True,
            "entries": [{"type": "phase", "id": "thinking", "order": 1,
                         "kind": "state", "phase": "thinking", "at": 1, "ended_at": 2}],
        }
        if guard_reason == "no_progress":
            details["terminal_assistant_message_content"] = json.dumps({"text": "Same result."})
        await stack.storage.update_agent_task(run.task_id, details=details)

    async with _open_goal_rpc_stack(
        tmp_path / "guard-atomic.sqlite", handler=handler, wire_lifecycle=True,
    ) as stack:
        service = stack.service
        commit = stack.storage._commit_transaction
        settle = stack.storage.settle_goal_task
        pause = stack.storage.pause_goal_for_system

        async def observe_settle(*args: Any, **kwargs: Any) -> Any:
            nonlocal armed
            armed = kwargs.get("guardrail_pause_reason") == guard_reason
            return await settle(*args, **kwargs)

        async def fail_guard_commit(
            conn: Any, operation: str, deadline: float, started: float,
        ) -> None:
            if operation == "settle_goal_task" and armed and not failed.is_set():
                # Fault after UPDATE but before COMMIT. The actual storage
                # transaction must roll back both pause and owner release.
                failed.set()
                raise StorageBusyError(operation, waited_ms=1, retry_after_ms=75)
            await commit(conn, operation, deadline, started)

        async def reject_separate_guard_write(*args: Any, **kwargs: Any) -> Any:
            assert kwargs.get("reason") not in {"empty_continuations", "no_progress"}
            return await pause(*args, **kwargs)

        monkeypatch.setattr(stack.storage, "settle_goal_task", observe_settle)
        monkeypatch.setattr(stack.storage, "_commit_transaction", fail_guard_commit)
        monkeypatch.setattr(stack.storage, "pause_goal_for_system", reject_separate_guard_write)
        monkeypatch.setattr(goal_module, "_IDLE_RETRY_BASE_SECONDS", 0.01)
        await _handle_goals_set(_set_params(), stack.context)
        await asyncio.wait_for(failed.wait(), timeout=10)
        owner = await stack.storage.get_goal(SOURCE_KEY)
        assert owner is not None and owner.status == "active" and owner.active_task_id == runs[-1]
        assert owner.turns_started == 4 and owner.turns_settled == 3
        streaks = (
            service._empty_automatic_turns if guard_reason == "empty_continuations"
            else service._no_progress_automatic_turns
        )
        assert streaks[owner.goal_id][2] == 2
        for _ in range(20):
            service.schedule_idle_evaluation(SOURCE_KEY)
        paused = await _wait_for_goal(
            stack.storage, lambda goal: goal.status == "paused" and goal.active_task_id is None,
        )
        await _drain(service, SOURCE_KEY)
        assert paused.pause_reason == guard_reason
        assert paused.turns_started == paused.turns_settled == len(runs) == 4
        assert owner.goal_id not in streaks
        assert len(await stack.storage.list_agent_tasks(session_key=SOURCE_KEY, limit=20)) == 4


async def test_reattach_preserves_exhaustion_and_explicit_resume_retains_retry_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_goal_rpc_stack(tmp_path / "resume-retry.sqlite") as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await _settle_set_task(stack, created)
        monkeypatch.setattr(goal_module, "_IDLE_RETRY_WINDOW_SECONDS", 0.03)
        calls = 0

        async def unavailable(_key: str) -> None:
            nonlocal calls
            calls += 1
            raise StorageBusyError("continuation", waited_ms=1, retry_after_ms=300_000)

        monkeypatch.setattr(stack.service, "_kick_if_idle", unavailable)
        stack.service.schedule_idle_evaluation(SOURCE_KEY)
        await _drain(stack.service, SOURCE_KEY)
        original = stack.service._idle_retries[SOURCE_KEY]
        assert original.exhausted
        lease = stack.service._leases[SOURCE_KEY]
        reattached = await _handle_goals_reattach(
            _reattach_params(created, continuity_token=created["continuityToken"]), stack.context,
        )
        await _drain(stack.service, SOURCE_KEY)
        assert stack.service._leases[SOURCE_KEY] is not lease
        assert stack.service._idle_retries[SOURCE_KEY] is original
        assert calls == 1

        paused = await _handle_goals_pause(
            _mutation_params(reattached["goal"], request_index=25), stack.context,
        )
        params = _mutation_params(paused["goal"], request_index=26)
        resumed = await _handle_goals_resume(params, stack.context)
        assert resumed["accepted"] is True
        restarted = stack.service._idle_retries[SOURCE_KEY]
        assert restarted is not original and restarted.not_before == original.not_before
        await _drain(stack.service, SOURCE_KEY)
        assert restarted.exhausted and calls == 1
        await _handle_goals_resume(params, stack.context)
        assert stack.service._idle_retries[SOURCE_KEY] is restarted
        assert calls == 1
