"""Goal retry continuity and detached authority through RPC and real SQLite."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from opensquilla.gateway import goal_service as goal_service_module
from opensquilla.gateway.auth import resolve_auth
from opensquilla.gateway.rpc_goals import (
    _handle_goals_clear,
    _handle_goals_pause,
    _handle_goals_reattach,
    _handle_goals_set,
)
from opensquilla.gateway.token_store import TokenStore
from opensquilla.gateway.websocket import get_registry
from opensquilla.session.models import AgentTaskStatus
from opensquilla.session.storage import StorageBusyError
from tests.test_gateway.test_goal_recovery import (
    _allow_goal_owner_release,
    _deny_goal_owner_release,
)
from tests.test_gateway.test_goal_rpc import (
    SOURCE_KEY,
    _goal_connection,
    _mutation_params,
    _open_goal_rpc_stack,
    _reattach_params,
    _set_params,
    _table_count,
    _wait_for_goal,
    _wait_until,
)


async def _join_kick(service) -> None:
    worker = service._kick_tasks.get(SOURCE_KEY)
    if worker is not None:
        await asyncio.wait_for(asyncio.shield(worker), timeout=5)


async def _detach(stack) -> None:
    stack.subscriptions.unsubscribe_messages(stack.context.conn_id, SOURCE_KEY)
    await stack.service.on_subscription_lost(stack.context.conn_id, SOURCE_KEY)


async def _reattach(stack, created) -> None:
    stack.subscriptions.subscribe_messages(stack.context.conn_id, SOURCE_KEY)
    await _handle_goals_reattach(
        _reattach_params(created, continuity_token=created["continuityToken"]),
        stack.context,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("suspension", ["transport", "pending_user"])
async def test_unfinished_idle_episode_survives_suspension_and_same_token_wakes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suspension: str,
) -> None:
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_BUDGET_SECONDS", 3.0)
    runs: list[str] = []
    attempts: list[float] = []
    healthy = False

    async def handler(run):
        runs.append(run.task_id)
        if run.input_mode == "goal_continuation":
            await stack.service.commit_model_status(
                run.goal_context, status="complete", reason=None
            )

    async with _open_goal_rpc_stack(
        tmp_path / "suspension.sqlite",
        handler=handler,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=5)
        await _wait_for_goal(stack.storage, lambda goal: goal.active_task_id is None)
        original = stack.service._prepare_execution_envelope

        async def dependency(*args, **kwargs):
            attempts.append(time.monotonic())
            if not healthy:
                raise StorageBusyError("controlled preflight", waited_ms=1, retry_after_ms=200)
            return await original(*args, **kwargs)

        monkeypatch.setattr(stack.service, "_prepare_execution_envelope", dependency)
        with capture_logs():
            stack.service.schedule_idle_evaluation(SOURCE_KEY)

            async def pending():
                return SOURCE_KEY in stack.service._idle_recovery_windows

            await _wait_until(pending)
            window = stack.service._idle_recovery_windows[SOURCE_KEY][1]
            deadline, not_before = window.expires_at, window.not_before
            healthy = True
            if suspension == "transport":
                await _detach(stack)
                await _join_kick(stack.service)
                for _ in range(3):
                    assert stack.service._idle_recovery_windows[SOURCE_KEY][1] is window
                    assert window.expires_at == deadline and window.not_before == not_before
                    assert len(runs) == 1 and not stack.runtime._reservations_by_session
                    await _reattach(stack, created)
                    await _detach(stack)
                    await _join_kick(stack.service)
                assert len(attempts) == 1
                await _reattach(stack, created)
            else:
                # Explicit ingress temporarily owns admission, without proving
                # that the suspended recovery episode has completed.
                async with stack.runtime.explicit_ingress_intent(SOURCE_KEY):
                    await _join_kick(stack.service)
                    for _ in range(3):
                        stack.service.schedule_idle_evaluation(SOURCE_KEY)
                        await _join_kick(stack.service)
                        assert stack.service._idle_recovery_windows[SOURCE_KEY][1] is window
                        assert window.expires_at == deadline and window.not_before == not_before
                        assert len(runs) == 1
                stack.service.schedule_idle_evaluation(SOURCE_KEY)
            completed = await _wait_for_goal(
                stack.storage,
                lambda goal: goal.status == "complete" and goal.active_task_id is None,
            )
        await _join_kick(stack.service)
        assert completed.turns_started == completed.turns_settled == len(runs) == 2
        assert all(attempt >= not_before for attempt in attempts[1:])
        assert await _table_count(stack.storage, "agent_tasks") == 2
        assert not stack.service._idle_recovery_windows


@pytest.mark.asyncio
async def test_expired_detached_window_can_resume_on_one_later_external_wake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_BUDGET_SECONDS", 0.25)
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_INITIAL_SECONDS", 0.02)
    runs: list[str] = []
    healthy = False

    async def handler(run):
        runs.append(run.task_id)
        if run.input_mode == "goal_continuation":
            await stack.service.commit_model_status(
                run.goal_context, status="complete", reason=None
            )

    async with _open_goal_rpc_stack(
        tmp_path / "expired-detached.sqlite",
        handler=handler,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=5)
        await _wait_for_goal(stack.storage, lambda goal: goal.active_task_id is None)
        original = stack.service._prepare_execution_envelope

        async def dependency(*args, **kwargs):
            if not healthy:
                raise StorageBusyError("controlled preflight", waited_ms=1, retry_after_ms=30)
            return await original(*args, **kwargs)

        monkeypatch.setattr(stack.service, "_prepare_execution_envelope", dependency)
        with capture_logs():
            stack.service.schedule_idle_evaluation(SOURCE_KEY)

            async def pending():
                return SOURCE_KEY in stack.service._idle_recovery_windows

            await _wait_until(pending)
            window = stack.service._idle_recovery_windows[SOURCE_KEY][1]
            await _detach(stack)
            await _join_kick(stack.service)
            assert stack.service._idle_recovery_windows[SOURCE_KEY][1] is window
            assert not stack.service._kick_tasks
            await asyncio.sleep(max(0, window.expires_at - time.monotonic()) + 0.01)
            assert len(runs) == 1 and not stack.service._kick_tasks
            healthy = True
            await _reattach(stack, created)
            goal = await _wait_for_goal(
                stack.storage,
                lambda item: item.status == "complete" and item.active_task_id is None,
            )
        await _join_kick(stack.service)
        assert goal.turns_started == goal.turns_settled == len(runs) == 2
        assert not stack.service._idle_recovery_windows


@pytest.mark.asyncio
async def test_later_episode_cannot_bypass_outstanding_retry_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_BUDGET_SECONDS", 0.08)
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_INITIAL_SECONDS", 0.01)
    attempts: list[float] = []
    runs: list[str] = []
    healthy = False

    async def handler(run):
        runs.append(run.task_id)
        if run.input_mode == "goal_continuation":
            await stack.service.commit_model_status(
                run.goal_context, status="complete", reason=None
            )

    async with _open_goal_rpc_stack(
        tmp_path / "retry-after.sqlite",
        handler=handler,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=5)
        await _wait_for_goal(stack.storage, lambda goal: goal.active_task_id is None)
        original = stack.service._prepare_execution_envelope

        async def dependency(*args, **kwargs):
            attempts.append(time.monotonic())
            if not healthy:
                raise StorageBusyError("controlled preflight", waited_ms=1, retry_after_ms=300)
            return await original(*args, **kwargs)

        monkeypatch.setattr(stack.service, "_prepare_execution_envelope", dependency)
        with capture_logs():
            stack.service.schedule_idle_evaluation(SOURCE_KEY)
            await _join_kick(stack.service)
            original_window = stack.service._idle_recovery_windows[SOURCE_KEY][1]
            assert original_window.exhausted and len(attempts) == 1
            stack.service.schedule_idle_evaluation(SOURCE_KEY)
            next_window = stack.service._idle_recovery_windows[SOURCE_KEY][1]
            assert next_window is not original_window
            assert next_window.not_before == original_window.not_before
            await _join_kick(stack.service)
            assert len(attempts) == len(runs) == 1
            assert next_window.exhausted
            healthy = True
            await asyncio.sleep(max(0, next_window.not_before - time.monotonic()) + 0.01)
            stack.service.schedule_idle_evaluation(SOURCE_KEY)
            goal = await _wait_for_goal(
                stack.storage,
                lambda item: item.status == "complete" and item.active_task_id is None,
            )
        await _join_kick(stack.service)
        assert len(attempts) == len(runs) == 2
        assert attempts[1] >= original_window.not_before
        assert goal.turns_started == goal.turns_settled == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["pause", "clear"])
async def test_revoked_goal_discards_suspended_recovery_without_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_INITIAL_SECONDS", 0.02)
    async with _open_goal_rpc_stack(
        tmp_path / "revoked-window.sqlite",
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=5)
        idle = await _wait_for_goal(stack.storage, lambda goal: goal.active_task_id is None)

        async def unavailable(*_args, **_kwargs):
            raise StorageBusyError("controlled preflight", waited_ms=1, retry_after_ms=30)

        monkeypatch.setattr(stack.service, "_prepare_execution_envelope", unavailable)
        with capture_logs():
            stack.service.schedule_idle_evaluation(SOURCE_KEY)

            async def pending():
                return SOURCE_KEY in stack.service._idle_recovery_windows

            await _wait_until(pending)
            await _detach(stack)
            await _join_kick(stack.service)
            command = _handle_goals_pause if mutation == "pause" else _handle_goals_clear
            await command(
                _mutation_params(
                    {"goalId": idle.goal_id, "stateRevision": idle.state_revision},
                    request_index=2,
                ),
                stack.context,
            )
            stack.service.schedule_idle_evaluation(SOURCE_KEY)
            await _join_kick(stack.service)
        assert not stack.service._idle_recovery_windows
        assert not stack.service._continuity_grants
        assert not stack.runtime._reservations_by_session
        assert await _table_count(stack.storage, "agent_tasks") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("wake_after_expiry", [False, True])
async def test_external_wake_during_inflight_attempt_only_renews_after_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wake_after_expiry: bool,
) -> None:
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_BUDGET_SECONDS", 0.5)
    monkeypatch.setattr(goal_service_module, "_GOAL_RECOVERY_INITIAL_SECONDS", 0.01)
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = 0
    runs: list[str] = []

    async def handler(run):
        runs.append(run.task_id)
        if run.input_mode == "goal_continuation":
            await stack.service.commit_model_status(
                run.goal_context, status="complete", reason=None
            )

    async with _open_goal_rpc_stack(
        tmp_path / "inflight-wake.sqlite",
        handler=handler,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=5)
        await _wait_for_goal(stack.storage, lambda goal: goal.active_task_id is None)
        original = stack.service._prepare_execution_envelope

        async def dependency(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                entered.set()
                await release.wait()
            if attempts <= 2:
                raise StorageBusyError("controlled preflight", waited_ms=1, retry_after_ms=10)
            return await original(*args, **kwargs)

        monkeypatch.setattr(stack.service, "_prepare_execution_envelope", dependency)
        with capture_logs():
            try:
                stack.service.schedule_idle_evaluation(SOURCE_KEY)
                await asyncio.wait_for(entered.wait(), timeout=5)
                worker = stack.service._kick_tasks[SOURCE_KEY]
                window = stack.service._idle_recovery_windows[SOURCE_KEY][1]
                if not wake_after_expiry:
                    assert time.monotonic() < window.expires_at
                    stack.service.schedule_idle_evaluation(SOURCE_KEY)
                    assert stack.service._idle_recovery_windows[SOURCE_KEY][1] is window
                await asyncio.sleep(max(0, window.expires_at - time.monotonic()) + 0.01)
                if wake_after_expiry:
                    stack.service.schedule_idle_evaluation(SOURCE_KEY)
                    next_window = stack.service._idle_recovery_windows[SOURCE_KEY][1]
                    assert next_window is not window
                    assert next_window.not_before == window.not_before
                    assert stack.service._kick_tasks[SOURCE_KEY] is worker
                release.set()
                await _join_kick(stack.service)
                if not wake_after_expiry:
                    assert attempts == 2 and len(runs) == 1
                    assert window.exhausted
                    stack.service.schedule_idle_evaluation(SOURCE_KEY)
                goal = await _wait_for_goal(
                    stack.storage,
                    lambda item: item.status == "complete" and item.active_task_id is None,
                )
                await _join_kick(stack.service)
                assert attempts == 3
                assert goal.turns_started == goal.turns_settled == len(runs) == 2
                assert not stack.service._idle_recovery_windows
            finally:
                release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["unavailable", "deny"])
@pytest.mark.parametrize("failed_defer", [False, True])
@pytest.mark.parametrize("transport", ["connection", "subscription", "subscription_during_read"])
async def test_detached_postaccept_real_authority_database_keeps_unavailable_distinct_from_deny(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
    failed_defer: bool,
    transport: str,
) -> None:
    runs: list[str] = []

    async def handler(run):
        runs.append(run.task_id)
        if run.input_mode == "goal_continuation":
            await stack.service.commit_model_status(
                run.goal_context, status="complete", reason=None
            )

    async with _open_goal_rpc_stack(
        tmp_path / "detached-goal.sqlite",
        handler=handler,
        wire_lifecycle=True,
        wire_idle=False,
    ) as stack:
        state = tmp_path / "auth"
        stack.context.config.state_dir = str(state)
        stack.context.config.auth.mode = "token"
        token_store = TokenStore(state / "sessions.db")
        issued = token_store.create(
            name="Isolated scheduler test",
            roles={"operator"},
            scopes={"operator.read", "operator.write"},
            capabilities={"host.execute", "task.read", "task.submit"},
        )
        principal = resolve_auth(
            stack.context.config,
            auth_params={"token": issued.token},
            role_claim="operator",
            peer_ip="192.168.1.7",
        )
        assert principal is not None and principal.authenticated
        connection = get_registry().get(stack.context.conn_id)
        stack.context.principal = connection.principal = principal
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=5)
        await _wait_for_goal(stack.storage, lambda goal: goal.active_task_id is None)
        original = stack.service._lease_for_async
        original_decision = stack.service._authority_decision
        reads = 0
        table_renamed = False
        fault_during_read = False

        async def lose_transport_and_authority():
            nonlocal table_renamed
            if failed_defer:
                await _deny_goal_owner_release(stack.storage, compensation_too=True)
            if transport == "connection":
                get_registry().unregister(stack.context.conn_id)
            else:
                stack.subscriptions.unsubscribe_messages(stack.context.conn_id, SOURCE_KEY)
            if authority == "unavailable":
                # Real SQL read failure in the actual authorization resolver.
                # Only this isolated test database is altered and restored.
                with sqlite3.connect(state / "sessions.db") as blocker:
                    blocker.execute("ALTER TABLE sandbox_tokens RENAME TO hidden_tokens")
                table_renamed = True
            else:
                assert token_store.revoke(issued.record.public_id)

        async def lose_authority_after_accept(goal):
            nonlocal reads, fault_during_read
            reads += 1
            if reads == 3:
                if transport == "subscription_during_read":
                    fault_during_read = True
                else:
                    await lose_transport_and_authority()
            return await original(goal)

        async def lose_subscription_during_read(principal):
            nonlocal fault_during_read
            if fault_during_read:
                fault_during_read = False
                await lose_transport_and_authority()
            return await original_decision(principal)

        monkeypatch.setattr(stack.service, "_lease_for_async", lose_authority_after_accept)
        monkeypatch.setattr(stack.service, "_authority_decision", lose_subscription_during_read)
        try:
            with capture_logs():
                stack.service.schedule_idle_evaluation(SOURCE_KEY)
                await _join_kick(stack.service)
            before = await stack.storage.get_goal(SOURCE_KEY)
            retained = stack.service._continuity_grants.get(SOURCE_KEY)
            assert len(runs) == 1 and not stack.runtime._reservations_by_session
            assert SOURCE_KEY not in stack.service._leases
            if authority == "unavailable":
                assert (
                    retained is not None and retained.continuity_token == created["continuityToken"]
                )
            else:
                assert retained is None
            if failed_defer:
                assert before.active_task_id is not None
                old_task_id = before.active_task_id
                old_task = await stack.storage.get_agent_task(old_task_id)
                assert old_task.status == AgentTaskStatus.QUEUED and old_task.started_at is None
                assert stack.service._settlement_recoveries
                await _allow_goal_owner_release(stack.storage)
            else:
                old_task_id = None
            repaired = await _wait_for_goal(stack.storage, lambda goal: goal.active_task_id is None)

            async def recovery_finished():
                return (
                    not stack.service._settlement_recoveries and not stack.service._settlement_tasks
                )

            await _wait_until(recovery_finished)
            await _join_kick(stack.service)
            assert repaired.turns_started == repaired.turns_settled == 2
            assert repaired.continuation_seq == 1
            assert len(runs) == 1
            if old_task_id is not None:
                old_task = await stack.storage.get_agent_task(old_task_id)
                assert old_task.status == AgentTaskStatus.ABANDONED and old_task.started_at is None
            assert not stack.service._settlement_recoveries
            if authority == "deny":
                assert repaired.status == "paused" and repaired.pause_reason == "activation_failed"
                assert SOURCE_KEY not in stack.service._continuity_grants
                return
            assert repaired.status == "active" and repaired.pause_reason is None
            with sqlite3.connect(state / "sessions.db") as blocker:
                blocker.execute("ALTER TABLE hidden_tokens RENAME TO sandbox_tokens")
            table_renamed = False
            if get_registry().get(stack.context.conn_id) is None:
                get_registry().register(_goal_connection(stack.context.conn_id, principal))
            await _reattach(stack, created)
            completed = await _wait_for_goal(
                stack.storage,
                lambda goal: goal.status == "complete" and goal.active_task_id is None,
            )
            await _join_kick(stack.service)
            assert completed.continuation_seq == 2
            assert completed.turns_started == completed.turns_settled == 3
            assert len(runs) == 2
            assert await _table_count(stack.storage, "agent_tasks") == 3
        finally:
            if table_renamed:
                with sqlite3.connect(state / "sessions.db") as blocker:
                    blocker.execute("ALTER TABLE hidden_tokens RENAME TO sandbox_tokens")
            if failed_defer:
                await _allow_goal_owner_release(stack.storage)
