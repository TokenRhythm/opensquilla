"""Progress persists on ordinary tasks and never controls their execution."""
from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus, SessionNode
from opensquilla.session.storage import SessionStorage, StaleEpochError
from opensquilla.tools.builtin.plan_control import update_plan
from opensquilla.tools.types import SafeToolError, ToolContext, current_tool_context

SESSION = "agent:main:webchat:progress-test"


@pytest_asyncio.fixture
async def storage():
    store = SessionStorage(":memory:")
    await store.connect()
    await store.upsert_session(SessionNode(
        session_key=SESSION, session_id="progress-session", agent_id="main", epoch=0,
    ))
    await store.create_agent_task(AgentTaskRecord(
        task_id="progress-task", session_key=SESSION, agent_id="main",
        source_kind="web", queue_mode="followup", run_kind="web_turn",
        status=AgentTaskStatus.RUNNING, details={"metadata": {"other": "preserved"}},
    ))
    try:
        yield store
    finally:
        await store.close()


async def _update(storage, steps, explanation=None, **overrides):
    args = dict(session_key=SESSION, session_id="progress-session", session_epoch=0)
    args.update(overrides)
    return await storage.update_task_progress(
        "progress-task", steps=steps, explanation=explanation, **args,
    )


async def test_progress_can_reorder_reopen_add_remove_and_clear(storage):
    initial = await _update(storage, [
        {"step": "Investigate", "status": "completed"},
        {"step": "Implement", "status": "in_progress"},
    ])
    changed = await _update(storage, [
        {"step": "Repair failing verification", "status": "in_progress"},
        {"step": "Investigate", "status": "pending"},
    ], "New evidence changed the approach")
    assert changed["revision"] == initial["revision"] + 1
    task = await storage.get_agent_task("progress-task")
    assert task.details["metadata"]["progress"] == changed
    assert task.details["metadata"]["other"] == "preserved"
    assert task.status == AgentTaskStatus.RUNNING
    cleared = await _update(storage, [])
    assert cleared["steps"] == []
    assert cleared["revision"] == 3


async def test_concurrent_replacements_have_monotonic_revisions(storage):
    changes = await asyncio.gather(*[
        _update(storage, [{"step": f"Investigation {i}", "status": "pending"}])
        for i in range(8)
    ])
    assert sorted(item["revision"] for item in changes) == list(range(1, 9))
    task = await storage.get_agent_task("progress-task")
    assert task.details["metadata"]["progress"]["revision"] == 8


@pytest.mark.parametrize("state", [AgentTaskStatus.SUCCEEDED, AgentTaskStatus.CANCELLED])
async def test_terminal_task_cannot_be_revived_by_progress(storage, state):
    await storage.update_agent_task("progress-task", status=state)
    with pytest.raises(ValueError, match="running task"):
        await _update(storage, [{"step": "Unexpected", "status": "in_progress"}])
    assert (await storage.get_agent_task("progress-task")).status == state


async def test_generation_fence_rejects_old_progress(storage):
    with pytest.raises(StaleEpochError):
        await _update(storage, [], session_epoch=1)
    with pytest.raises(StaleEpochError):
        await _update(storage, [], session_id="replacement-session")


async def test_tool_uses_shared_durable_progress(storage):
    async def callback(steps, explanation):
        return await _update(storage, steps, explanation)

    context = ToolContext(update_progress=callback)
    token = current_tool_context.set(context)
    try:
        result = json.loads(await update_plan([{"step": "Verify", "status": "completed"}]))
        assert result["status"] == "accepted"
        task = await storage.get_agent_task("progress-task")
        assert task.details["metadata"]["progress"] == result["progress"]
        context.subagent_depth = 1
        with pytest.raises(SafeToolError):
            await update_plan([])
        context.subagent_depth = 0
        context.collaboration_mode = "plan"
        with pytest.raises(SafeToolError):
            await update_plan([])
    finally:
        current_tool_context.reset(token)


async def test_progress_bounds_remain_enforced(storage):
    with pytest.raises(ValueError):
        await _update(storage, [{"step": str(i), "status": "pending"} for i in range(21)])
    with pytest.raises(ValueError):
        await _update(storage, [{"step": "Verify", "status": "invented"}])
    assert "progress" not in (await storage.get_agent_task("progress-task")).details["metadata"]
