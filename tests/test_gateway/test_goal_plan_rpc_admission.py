"""Goal RPC entrypoints share task locks, not historical PlanRun state."""

import asyncio

import pytest

from opensquilla.gateway.rpc import RpcHandlerError
from opensquilla.gateway.rpc_goals import _handle_goals_set
from opensquilla.gateway.rpc_sessions import _handle_sessions_send_contract
from opensquilla.session.goals import GoalTurnContext
from tests.test_gateway.test_goal_rpc import (
    SOURCE_KEY,
    _open_goal_rpc_stack,
    _set_params,
    _wait_for_goal,
)
from tests.test_session.test_goal_plan_admission import _legacy_plan_projection


@pytest.mark.parametrize("plan_status", ["queued", "running", "paused", "blocked"])
async def test_goal_service_and_user_turn_ignore_orphan_plan_projection(tmp_path, plan_status):
    contexts = []

    async def handler(run):
        contexts.append(GoalTurnContext.from_task_detail(run.goal_context))

    async with _open_goal_rpc_stack(
        tmp_path / "historical-plan-goal.sqlite", handler=handler,
        wire_lifecycle=True, wire_idle=False,
    ) as stack:
        session = await stack.storage.get_session(SOURCE_KEY)
        assert session is not None
        await _legacy_plan_projection(
            stack.storage, session_key=SOURCE_KEY, session_id=session.session_id,
            status=plan_status,
        )
        created = await _handle_goals_set(_set_params(), stack.context)
        await stack.runtime.wait(created["taskId"], timeout=2)
        await _wait_for_goal(stack.storage, lambda goal: goal.active_task_id is None)
        sent = await _handle_sessions_send_contract(
            {"key": SOURCE_KEY, "message": "Continue the synthetic work.",
             "clientRequestId": "historical-plan-followup"},
            stack.context,
        )
        await stack.runtime.wait(sent["task_id"], timeout=2)
        assert len(contexts) == 2
        assert all(ctx is not None and ctx.goal_id == created["goal"]["goalId"] for ctx in contexts)
        assert contexts[1].task_id == sent["task_id"]


async def test_goal_service_uses_real_running_task_lock_with_orphan_plan(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(run):
        started.set()
        await release.wait()

    async with _open_goal_rpc_stack(
        tmp_path / "real-plan-task-lock.sqlite", handler=handler,
    ) as stack:
        session = await stack.storage.get_session(SOURCE_KEY)
        assert session is not None
        await _legacy_plan_projection(
            stack.storage, session_key=SOURCE_KEY, session_id=session.session_id,
            status="blocked",
        )
        sent = await _handle_sessions_send_contract(
            {"key": SOURCE_KEY, "message": "Run the synthetic task.",
             "clientRequestId": "real-task-blocker"},
            stack.context,
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        await stack.storage.conn.execute(
            "UPDATE plan_runs SET status = 'running', active_task_id = ?", (sent["task_id"],),
        )
        await stack.storage.conn.commit()
        try:
            with pytest.raises(RpcHandlerError) as exc:
                await _handle_goals_set(_set_params(), stack.context)
            assert exc.value.code == "GOAL_BUSY"
            assert await stack.storage.get_goal(SOURCE_KEY) is None
        finally:
            release.set()
            await stack.runtime.wait(sent["task_id"], timeout=2)
