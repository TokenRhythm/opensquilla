"""Shared web/channel admission retains exact Goal task cleanup after commit."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.goal_service import GoalService
from opensquilla.gateway.websocket import SubscriptionManager
from opensquilla.session.goals import effective_goal_turn_context
from opensquilla.session.models import AgentTaskStatus
from tests.test_gateway.test_goal_activation_recovery import (
    _assert_admission_released,
    _wait_for_recovery_drain,
)
from tests.test_gateway.test_goal_recovery import (
    _allow_goal_owner_release,
    _deny_goal_owner_release,
)
from tests.test_gateway.test_turn_activation_failures import (
    ingress,
    seed_channel_goal,
    seed_web_goal,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["web", "channel"])
@pytest.mark.parametrize(
    "failure", ["both_cleanup", "goal_only", "lookup_unavailable", "goal_removed"],
)
async def test_goal_owned_ingress_retains_cleanup_using_committed_receipt_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str, failure: str,
) -> None:
    async with ingress(tmp_path, surface) as (stack, send):
        goal = await (seed_web_goal if surface == "web" else seed_channel_goal)(stack)

        async def emit(*_args) -> None:
            pass

        service = GoalService(
            storage=stack.storage, session_manager=stack.manager,
            task_runtime=stack.runtime, event_emitter=emit,
            subscription_manager=SubscriptionManager(),
            config=GatewayConfig(workspace_dir=str(tmp_path / "workspace")),
        )
        stack.runtime.set_goal_service(service)

        async def unavailable_goal_lookup(*_args, **_kwargs):
            raise sqlite3.OperationalError("controlled current Goal lookup outage")

        async def fail_activation(reservation, **_kwargs):
            assert not reservation.activated
            if failure == "goal_only":
                await _deny_goal_owner_release(stack.storage, compensation_too=True)
            else:
                await stack.storage.conn.execute("""CREATE TRIGGER deny_unstarted_cleanup
                    BEFORE UPDATE OF status ON agent_tasks WHEN OLD.status = 'queued'
                    BEGIN SELECT RAISE(FAIL, 'controlled unstarted cleanup outage'); END""")
                await stack.storage.conn.commit()
            if failure == "lookup_unavailable":
                monkeypatch.setattr(stack.storage, "get_goal_by_id", unavailable_goal_lookup)
            elif failure == "goal_removed":
                # Clear is a separate Goal lifecycle boundary. The committed
                # receipt still owns this exact task's cleanup obligation.
                await service.clear(
                    session_key=goal.session_key, expected_goal_id=goal.goal_id,
                    expected_state_revision=goal.state_revision + 1,
                    client_request_id="f0506582-4d4f-4631-8a1f-1b0fc40734a7",
                    source_scope="web:isolated-test",
                )
            raise RuntimeError("controlled activation failure before runtime ownership")

        monkeypatch.setattr(stack.runtime, "activate", fail_activation)
        try:
            with capture_logs():
                task_id, status, replayed = await send()
            task_before = await stack.storage.get_agent_task(task_id)
            context = effective_goal_turn_context(task_before.details)
            assert context is not None and context.goal_id == goal.goal_id
            assert "goal_candidate" not in task_before.details
            assert not replayed
            assert service._settlement_recoveries
            assert all(
                pending.event.session_key == goal.session_key
                for pending in service._settlement_recoveries.values()
            )
            expected_status = (
                AgentTaskStatus.FAILED if failure == "goal_only" else AgentTaskStatus.QUEUED
            )
            assert task_before.status == status == expected_status
            assert not stack.runtime._reservations_by_session
            assert not stack.runtime._tasks and not stack.received_runs
            if failure == "goal_only":
                _assert_admission_released(task_before)
                await _allow_goal_owner_release(stack.storage)
            else:
                await stack.storage.conn.execute("DROP TRIGGER deny_unstarted_cleanup")
                await stack.storage.conn.commit()
            # No additional ingress or idle wake is needed to release the task.
            await _wait_for_recovery_drain(service)
            task = await stack.storage.get_agent_task(task_id)
            _assert_admission_released(task)
            assert task.started_at is None
            if failure == "goal_only":
                assert task == task_before  # Preserve the fallback's actual terminal record.
            else:
                assert task.status == AgentTaskStatus.ABANDONED
            healed = await stack.storage.get_goal(goal.session_key)
            if failure == "goal_removed":
                assert healed is None
            else:
                assert healed.active_task_id is None
                assert healed.status == "paused" and healed.pause_reason == "activation_failed"
                assert healed.turns_started == healed.turns_settled == goal.turns_settled + 1
            replay_id, replay_status, replayed = await send()
            assert replayed and replay_id == task_id and replay_status == task.status
            assert await stack.storage.get_goal(goal.session_key) == healed
            assert await stack.storage.get_agent_task(task_id) == task
            assert not stack.received_runs
        finally:
            await service.close()
