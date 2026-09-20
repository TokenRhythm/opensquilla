"""Goal continuations must not overtake a pending interactive tool response."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from opensquilla import token_estimation
from opensquilla.contracts.gateway_transport import STRUCTURED_USER_INPUT_CAPABILITY
from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.boot import dispatch_task_runtime_turn
from opensquilla.gateway.config import (
    AttachmentsConfig,
    GatewayConfig,
    GoalConfig,
    SquillaRouterConfig,
)
from opensquilla.gateway.rpc_chat import _submit_clarification
from opensquilla.gateway.rpc_goals import _handle_goals_set
from opensquilla.gateway.rpc_sessions import _handle_sessions_bootstrap
from opensquilla.gateway.task_runtime import TaskRun
from opensquilla.gateway.websocket import get_registry
from opensquilla.provider import DoneEvent, TextDeltaEvent, ToolUseEndEvent, ToolUseStartEvent
from opensquilla.session.models import AgentTaskStatus
from opensquilla.tools.registry import ToolRegistry, get_default_registry
from tests.test_gateway.test_goal_rpc import (
    SOURCE_KEY,
    _DurableGoalArtifactSelector,
    _open_goal_rpc_stack,
    _set_params,
    _table_count,
    _wait_for_goal,
)


class _QuestionProvider:
    provider_name = "test"
    model = "test/model"

    def __init__(self, *, automatic: bool) -> None:
        self.calls = 0
        self.automatic = automatic

    async def list_models(self) -> list[Any]:
        return []

    async def chat(self, messages: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        self.calls += 1
        if self.automatic and self.calls == 1:
            yield TextDeltaEvent(text="I will obtain the missing target next.")
            yield DoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        if self.calls == 1 + self.automatic:
            name = "request_user_input"
            arguments = {"questions": [{"id": "target", "question": "Which target?"}]}
        elif self.calls == 2 + self.automatic:
            assert "synthetic-blue-target" in str(messages)
            name = "update_goal"
            arguments = {"status": "complete"}
        else:
            yield TextDeltaEvent(text="Selected synthetic-blue-target. Goal complete.")
            yield DoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        tool_id = f"question-goal-{self.calls}"
        yield ToolUseStartEvent(tool_use_id=tool_id, tool_name=name)
        yield ToolUseEndEvent(tool_use_id=tool_id, tool_name=name, arguments=arguments)
        yield DoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)


@pytest.mark.parametrize("source_kind", ["cli", "web"])
@pytest.mark.parametrize("automatic", [False, True], ids=["initial", "continuation"])
@pytest.mark.parametrize("cancel", [False, True], ids=["answer", "cancel"])
async def test_goal_waits_for_user_input_in_original_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str, cancel: bool,
    automatic: bool,
) -> None:
    # Keep offline lifecycle tests independent of tokenizer downloads.
    monkeypatch.setattr(token_estimation, "_get_encoding", lambda: None)
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    state: dict[str, Any] = {}
    published = asyncio.Event()
    task_ids: list[str] = []

    async def emit(_key: str, name: str, payload: dict[str, Any]) -> None:
        if (
            name == "session.event.tool_result"
            and payload.get("name", payload.get("tool_name")) == "request_user_input"
        ):
            published.set()

    async def handler(run: TaskRun) -> None:
        task_ids.append(run.task_id)
        await dispatch_task_runtime_turn(
            run, config=state["config"], session_manager=state["manager"],
            turn_runner=state["runner"], event_emitter=emit,
        )

    async with _open_goal_rpc_stack(
        tmp_path / "goal-input.sqlite", handler=handler, wire_lifecycle=True,
    ) as stack:
        connection = get_registry().get(stack.context.conn_id)
        assert connection is not None
        connection.client_caps = frozenset({STRUCTURED_USER_INPUT_CAPABILITY})
        provider = _QuestionProvider(automatic=automatic)
        config = GatewayConfig(
            workspace_dir=str(tmp_path / "workspace"),
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            memory={}, naming={"enabled": False},
            goal=GoalConfig(execution_enabled=True),
            squilla_router=SquillaRouterConfig(enabled=False),
            agent_max_provider_retries=0,
        )
        registry = ToolRegistry()
        for name in ("request_user_input", "update_goal"):
            registered = get_default_registry().get(name)
            assert registered is not None
            registry.register(registered.spec, registered.handler)
        runner = TurnRunner(
            provider_selector=_DurableGoalArtifactSelector(provider),
            tool_registry=registry, session_manager=stack.manager, config=config,
        )
        runner.set_session_lock_provider(stack.runtime._get_session_lock_for_turn)
        state.update(config=config, manager=stack.manager, runner=runner)
        await stack.manager.update(SOURCE_KEY, model="test/model")
        await _handle_goals_set(
            {**_set_params(objective="Obtain the user's target and report it."),
             "sourceKind": source_kind}, stack.context,
        )
        await asyncio.wait_for(published.wait(), 3)
        pending = stack.runtime.pending_user_inputs(SOURCE_KEY)
        assert len(pending) == 1
        snapshot = await _handle_sessions_bootstrap({"key": SOURCE_KEY}, stack.context)
        assert snapshot["session"]["pendingUserInputs"] == pending
        request_id = pending[0]["request_id"]
        await stack.service._kick_if_idle(SOURCE_KEY)
        waiting_task_id = pending[0]["run_id"]
        first = await stack.storage.get_agent_task(waiting_task_id)
        goal = await stack.storage.get_goal(SOURCE_KEY)
        assert first is not None and first.status == AgentTaskStatus.RUNNING
        assert goal is not None and goal.status == "active"
        assert goal.active_task_id == first.task_id
        assert goal.continuation_seq == int(automatic)
        assert goal.turns_started == 1 + automatic
        assert task_ids[-1] == first.task_id
        assert len(task_ids) == 1 + automatic
        assert provider.calls == 1 + automatic
        assert stack.runtime._global_in_flight == 0

        if cancel:
            await stack.runtime.cancel(task_id=first.task_id)
            terminal = await stack.runtime.wait(first.task_id, timeout=3)
            assert terminal.status == AgentTaskStatus.CANCELLED
            assert provider.calls == 1 + automatic
        else:
            params = {
                "sessionKey": SOURCE_KEY, "request_id": request_id,
                "fields": {"target": "synthetic-blue-target"},
            }
            resolved = await _submit_clarification(params, stack.context)
            replayed = await _submit_clarification(params, stack.context)
            assert resolved["resolved"] and not resolved["replayed"]
            assert replayed["replayed"]
            terminal = await stack.runtime.wait(first.task_id, timeout=3)
            goal = await _wait_for_goal(
                stack.storage,
                lambda current: current.status == "complete" and current.active_task_id is None,
            )
            assert terminal.status == AgentTaskStatus.SUCCEEDED
            assert goal.terminal_task_id == first.task_id
            assert goal.turns_started == goal.turns_settled == 1 + automatic
            assert provider.calls == 3 + automatic

        assert stack.runtime.pending_user_inputs(SOURCE_KEY) == []
        snapshot = await _handle_sessions_bootstrap({"key": SOURCE_KEY}, stack.context)
        assert snapshot["session"]["pendingUserInputs"] == []
        assert task_ids[-1] == first.task_id
        assert len(task_ids) == 1 + automatic
        assert await _table_count(stack.storage, "agent_tasks") == 1 + automatic
