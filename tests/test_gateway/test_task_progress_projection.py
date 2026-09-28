import json
from types import SimpleNamespace

import pytest

from opensquilla.gateway.rpc_sessions import _task_summary


def test_task_summary_projects_common_progress_without_unrelated_task_metadata():
    progress = {
        "revision": 2,
        "explanation": "Inspection is complete.",
        "steps": [{"step": "Inspect files", "status": "completed"}],
    }
    result = _task_summary(SimpleNamespace(
        task_id="ordinary-task",
        status="running",
        details={"metadata": {"progress": progress, "internal_context": "private"}},
    ))

    assert result["task_id"] == "ordinary-task"
    assert result["progress"] == progress
    assert "metadata" not in result
    assert "internal_context" not in result


@pytest.mark.parametrize("status", ["submitted", "discussion"])
def test_task_summary_projects_plan_result_with_only_public_revision_fields(status):
    result = _task_summary(SimpleNamespace(details={"metadata": {"plan_result": {
        "status": status,
        "previousRevisionId": None,
        "revisionId": "revision-current",
        "internal_context": "private",
    }}}))

    assert result["plan_result"] == {
        "status": status, "previousRevisionId": None, "revisionId": "revision-current",
    }


def test_task_summary_ignores_unknown_plan_result_status():
    result = _task_summary(SimpleNamespace(details={"metadata": {
        "plan_result": {"status": "future", "revisionId": "revision-current"},
    }}))
    assert "plan_result" not in result


@pytest.mark.parametrize("create_before_discovery", [False, True])
async def test_discovered_progress_projects_ordinary_task_and_naturally_created_and_edited_goal(
    tmp_path, create_before_discovery,
):
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.engine.types import ToolCall
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.rpc_sessions import _handle_sessions_send_contract
    from opensquilla.session.models import AgentTaskStatus
    from opensquilla.tools.registry import get_default_registry
    from tests.test_gateway.test_goal_rpc import SOURCE_KEY, _open_goal_rpc_stack

    checked = []
    observed_event_counts = []

    async def handler(run):
        ctx = run.envelope.tool_context(is_owner=True)
        runner = TurnRunner(
            provider_selector=None, config=GatewayConfig(),
            tool_registry=get_default_registry(),
        )
        definitions, dispatch = runner._build_tools(ctx)
        assert "update_plan" not in {definition.name for definition in definitions}
        assert "update_plan" in ctx.authorized_tool_names
        agent = Agent(
            provider=object(), tool_definitions=definitions, tool_handler=dispatch,
            tool_registry=get_default_registry(), tool_context=ctx,
        )
        tool_call_count = 0

        async def invoke(name, arguments):
            nonlocal tool_call_count
            tool_call_count += 1
            result = await agent._execute_tool(ToolCall(
                tool_use_id=f"call-{tool_call_count}", tool_name=name, arguments=arguments,
            ))
            assert not result.is_error, result.content
            return json.loads(result.content)

        if create_before_discovery:
            await invoke("create_goal", {"objective": "Complete the synthetic task."})
        found = await invoke("tool_search", {"query": "update_plan", "limit": 1})
        assert [match["name"] for match in found["matches"]] == ["update_plan"]
        assert "update_plan" in {definition.name for definition in agent.tool_definitions}
        if not create_before_discovery:
            ordinary = await invoke("update_plan", {
                "steps": [{"step": "Inspect the task", "status": "in_progress"}],
            })
            assert ordinary["progress"]["revision"] == 1
            assert await stack.storage.get_goal(SOURCE_KEY) is None
            assert not any(name == "session.event.goal" for _, name, _ in stack.events)
            await invoke("create_goal", {"objective": "Complete the synthetic task."})
        for index in (1, 2):
            if index == 2:
                await invoke("update_goal", {"objective": "Complete the revised synthetic task."})
            before = len(stack.events)
            steps = [{"step": f"Verification {index}", "status": "in_progress"}]
            result = await invoke("update_plan", {"steps": steps})
            events = [payload for _, name, payload in stack.events[before:]
                      if name == "session.event.goal"]
            observed_event_counts.append(len(events))
            assert len(events) == 1
            projected = events[0]["goal"]
            assert projected["progress"]["steps"] == steps
            assert projected["objectiveRevision"] == index
            assert projected["activeTaskId"] == run.task_id
            assert result["progress"]["steps"] == projected["progress"]["steps"]
            task = await stack.storage.get_agent_task(run.task_id)
            assert task.status == AgentTaskStatus.RUNNING
            assert task.details["metadata"]["progress"]["steps"] == steps
            checked.append(index)

    async with _open_goal_rpc_stack(tmp_path / "goal-progress.sqlite", handler=handler) as stack:
        sent = await _handle_sessions_send_contract(
            {"key": SOURCE_KEY, "message": "Complete the synthetic task.",
             "clientRequestId": "public-goal-progress"}, stack.context,
        )
        task = await stack.runtime.wait(sent["task_id"], timeout=3)
        assert task.status == AgentTaskStatus.SUCCEEDED, task.error_message
        assert observed_event_counts == [1, 1]
        assert checked == [1, 2]


@pytest.mark.parametrize("replace_goal", [False, True])
async def test_progress_cannot_publish_goal_cleared_before_observer_runs(
    tmp_path, replace_goal,
):
    from opensquilla.gateway.rpc_goals import _handle_goals_clear, _handle_goals_set
    from opensquilla.gateway.rpc_sessions import _handle_sessions_send_contract
    from opensquilla.session.goals import GoalConflictError
    from opensquilla.session.models import AgentTaskStatus
    from opensquilla.tools.builtin.plan_control import update_plan
    from opensquilla.tools.types import ToolContext, current_tool_context
    from tests.test_gateway.test_goal_rpc import (
        SOURCE_KEY,
        _mutation_params,
        _open_goal_rpc_stack,
        _set_params,
    )

    started = False
    checked = []
    contexts = []

    async def handler(run):
        nonlocal started
        if started:
            return
        started = True
        ctx = ToolContext(
            session_key=SOURCE_KEY, session_id=run.envelope.session_id,
            session_epoch=run.envelope.session_epoch, task_id=run.task_id,
            update_progress=run.envelope.runtime_services["update_progress"],
        )
        goal = await stack.service.create_from_turn(ctx, objective="Original synthetic objective.")
        contexts.append(ctx.goal_context)

        async def change_goal(_key, name, _payload):
            if name != "session.event.progress":
                return
            await _handle_goals_clear(_mutation_params(goal, request_index=1), stack.context)
            stack.events.clear()

        stack.runtime._event_emitter = change_goal
        token = current_tool_context.set(ctx)
        try:
            result = json.loads(await update_plan([{"step": "Verify", "status": "completed"}]))
        finally:
            current_tool_context.reset(token)
        assert result["status"] == "accepted"
        assert stack.events == []
        current = await stack.storage.get_goal(SOURCE_KEY)
        assert current is None
        checked.append(True)

    async with _open_goal_rpc_stack(tmp_path / "cleared-progress.sqlite", handler=handler) as stack:
        sent = await _handle_sessions_send_contract(
            {"key": SOURCE_KEY, "message": "Complete the synthetic task.",
             "clientRequestId": "cleared-goal-progress"}, stack.context,
        )
        task = await stack.runtime.wait(sent["task_id"], timeout=3)
        assert task.status == AgentTaskStatus.SUCCEEDED
        assert checked == [True]
        if replace_goal:
            replacement = await _handle_goals_set(_set_params(request_index=2), stack.context)
            await stack.runtime.wait(replacement["taskId"], timeout=3)
            stack.events.clear()
            with pytest.raises(GoalConflictError):
                await stack.service.progress_updated(contexts[0], session_key=SOURCE_KEY)
            assert stack.events == []
            current = await stack.storage.get_goal(SOURCE_KEY)
            assert current.goal_id != contexts[0]["goalId"]
            assert current.progress_json is None


async def test_ordinary_progress_does_not_query_goal_storage(tmp_path, monkeypatch):
    from opensquilla.gateway.rpc_sessions import _handle_sessions_send_contract
    from opensquilla.session.models import AgentTaskStatus
    from opensquilla.tools.builtin.plan_control import update_plan
    from opensquilla.tools.types import ToolContext, current_tool_context
    from tests.test_gateway.test_goal_rpc import SOURCE_KEY, _open_goal_rpc_stack

    checked = []
    queries = []

    async def handler(run):
        async def unexpected_query(*_args, **_kwargs):
            queries.append(True)
            raise AssertionError("Ordinary progress must not query Goals")

        ctx = ToolContext(update_progress=run.envelope.runtime_services["update_progress"])
        token = current_tool_context.set(ctx)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(stack.storage, "get_goal", unexpected_query)
                patch.setattr(stack.storage, "get_goal_by_id", unexpected_query)
                result = json.loads(await update_plan([{"step": "Verify", "status": "completed"}]))
        finally:
            current_tool_context.reset(token)
        assert result["status"] == "accepted"
        assert queries == []
        assert stack.events == []
        checked.append(True)

    async with _open_goal_rpc_stack(
        tmp_path / "ordinary-progress.sqlite", handler=handler,
    ) as stack:
        sent = await _handle_sessions_send_contract(
            {"key": SOURCE_KEY, "message": "Complete the ordinary task.",
             "clientRequestId": "ordinary-progress"}, stack.context,
        )
        task = await stack.runtime.wait(sent["task_id"], timeout=3)
        assert task.status == AgentTaskStatus.SUCCEEDED
        assert checked == [True]
