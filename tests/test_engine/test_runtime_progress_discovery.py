"""Ordinary progress is discoverable without occupying the initial tool schema."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import ToolCall
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider import (
    ChatConfig,
    DoneEvent,
    Message,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import CallerKind, ToolContext


def _runner(config: GatewayConfig | None = None) -> TurnRunner:
    runner = TurnRunner(provider_selector=None, config=config or GatewayConfig())
    runner._tool_registry = get_default_registry()
    return runner


@pytest.mark.parametrize(
    "caller_kind", [CallerKind.AGENT, CallerKind.WEB, CallerKind.CLI, CallerKind.CHANNEL],
)
@pytest.mark.parametrize("has_goal_service", [False, True])
def test_ordinary_default_progress_starts_discoverable_not_surfaced(
    caller_kind: CallerKind, has_goal_service: bool,
) -> None:
    ctx = ToolContext(
        is_owner=True,
        caller_kind=caller_kind,
        channel_admin_verified=caller_kind is CallerKind.CHANNEL,
        goal_service=object() if has_goal_service else None,
    )

    definitions, _handler = _runner()._build_tools(ctx)

    names = {tool.name for tool in definitions}
    assert "tool_search" in names
    assert "request_user_input" in names
    assert "update_plan" not in names
    assert "update_plan" not in (ctx.surfaced_tools or set())
    assert "update_plan" in ctx.authorized_tool_names
    assert ctx.tool_search_index.search("update_plan", limit=1)[0].name == "update_plan"


@pytest.mark.parametrize("discovered", [False, True])
def test_rebuilding_ordinary_context_keeps_discovery_not_stale_eager_surface(
    discovered: bool,
) -> None:
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        surfaced_tools={"update_plan"},
        disclosed_tool_names={"update_plan"} if discovered else set(),
    )

    definitions, _handler = _runner()._build_tools(ctx)

    assert ("update_plan" in {tool.name for tool in definitions}) is discovered
    assert "update_plan" not in ctx.surfaced_tools
    assert "update_plan" in ctx.authorized_tool_names


@pytest.mark.parametrize(
    "authority",
    [
        pytest.param({"plan_run_id": "synthetic-run"}, id="plan-implementation"),
        pytest.param({"goal_context": {"goalId": "synthetic-goal"}}, id="goal-owned-turn"),
    ],
)
def test_owned_execution_keeps_progress_in_initial_schema(authority: dict[str, Any]) -> None:
    ctx = ToolContext(is_owner=True, caller_kind=CallerKind.WEB, **authority)

    definitions, _handler = _runner()._build_tools(ctx)

    assert "update_plan" in {tool.name for tool in definitions}
    assert "update_plan" in ctx.authorized_tool_names
    assert "update_plan" in ctx.surfaced_tools


@pytest.mark.parametrize(
    "context",
    [
        pytest.param({"collaboration_mode": "plan"}, id="plan-mode"),
        pytest.param({"caller_kind": CallerKind.SUBAGENT}, id="subagent-caller"),
        pytest.param({"subagent_depth": 1}, id="nested-agent"),
        pytest.param({"caller_kind": CallerKind.CRON}, id="cron-caller"),
    ],
)
def test_discovery_does_not_grant_progress_outside_main_default(
    context: dict[str, Any],
) -> None:
    ctx = ToolContext(is_owner=True, **context)

    definitions, _handler = _runner()._build_tools(ctx)

    names = {tool.name for tool in definitions}
    assert "update_plan" not in names
    assert "update_plan" not in ctx.authorized_tool_names
    assert all(hit.name != "update_plan" for hit in ctx.tool_search_index.search("update_plan"))
    if ctx.collaboration_mode == "plan":
        assert {"submit_plan", "request_user_input"} <= names


@pytest.mark.parametrize(
    ("context", "config"),
    [
        pytest.param({"denied_tools": {"update_plan"}}, {}, id="explicit-deny"),
        pytest.param({"allowed_tools": {"read_file"}}, {}, id="strict-allowlist"),
        pytest.param({"allowed_tools": set()}, {}, id="empty-allowlist"),
        pytest.param({}, {"tools": {"profile": "repo_coding_scaffold_edit"}}, id="profile"),
        pytest.param(
            {"is_owner": False, "caller_kind": CallerKind.CHANNEL}, {}, id="channel-non-owner",
        ),
        pytest.param({"guest_safe": True}, {}, id="guest-safe"),
    ],
)
async def test_progress_discovery_and_dispatch_preserve_authorization(
    context: dict[str, Any], config: dict[str, Any],
) -> None:
    updates: list[object] = []

    async def update_progress(steps: list[dict[str, str]], explanation: str | None) -> dict:
        updates.append((steps, explanation))
        return {"revision": 1, "steps": steps}

    ctx = ToolContext(**{"is_owner": True, **context}, update_progress=update_progress)
    runner = _runner(GatewayConfig(**config))
    definitions, handler = runner._build_tools(ctx)
    agent = Agent(
        provider=object(), tool_definitions=definitions, tool_handler=handler,
        tool_registry=runner._tool_registry, tool_context=ctx,
    )

    assert "update_plan" not in ctx.authorized_tool_names
    search = await agent._execute_tool(ToolCall(
        tool_use_id="search-denied", tool_name="tool_search", arguments={"query": "update_plan"},
    ))
    # Guest-safe sessions reject discovery itself as well as the target tool.
    assert search.is_error is ctx.guest_safe
    assert "update_plan" not in ctx.disclosed_tool_names
    assert "update_plan" not in {tool.name for tool in agent.tool_definitions}
    rejected = await agent._execute_tool(ToolCall(
        tool_use_id="progress-denied", tool_name="update_plan", arguments={"steps": []},
    ))
    assert rejected.is_error is True
    assert updates == []


@pytest.mark.parametrize("strict_allowlist", [False, True])
async def test_agent_discovers_progress_then_calls_real_handler_in_next_request(
    tmp_path: Path, strict_allowlist: bool,
) -> None:
    steps = [{"step": "Inspect the synthetic input", "status": "in_progress"}]
    explanation = "Track the requested work."
    updates: list[tuple[list[dict[str, str]], str | None]] = []

    async def update_progress(value: list[dict[str, str]], reason: str | None) -> dict:
        updates.append((value, reason))
        return {"revision": len(updates), "steps": value, "explanation": reason}

    class ProgressiveProvider:
        provider_name = "synthetic-progress-provider"
        model = "synthetic-progress-model"

        def __init__(self) -> None:
            self.tool_names_by_call: list[set[str]] = []

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            del messages, config
            call_index = len(self.tool_names_by_call)
            self.tool_names_by_call.append({tool.name for tool in tools or []})
            return self._stream(call_index)

        async def _stream(self, call_index: int) -> AsyncIterator[Any]:
            if call_index < 2:
                name = "tool_search" if call_index == 0 else "update_plan"
                arguments = (
                    {"query": "update_plan", "limit": 1}
                    if call_index == 0 else {"steps": steps, "explanation": explanation}
                )
                tool_use_id = f"synthetic-progress-{call_index}"
                yield ToolUseStartEvent(tool_use_id=tool_use_id, tool_name=name)
                yield ToolUseEndEvent(
                    tool_use_id=tool_use_id, tool_name=name, arguments=arguments,
                )
                yield DoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)
                return
            yield TextDeltaEvent(text="Synthetic task complete.")
            yield DoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)

        async def list_models(self) -> list[Any]:
            return []

    ctx = ToolContext(
        is_owner=True, caller_kind=CallerKind.WEB, workspace_dir=str(tmp_path),
        task_id="synthetic-progress-task", update_progress=update_progress,
        allowed_tools={"update_plan"} if strict_allowlist else None,
    )
    runner = _runner()
    definitions, handler = runner._build_tools(ctx)
    provider = ProgressiveProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=3, workspace_dir=str(tmp_path), model_id=provider.model,
        ),
        tool_definitions=definitions, tool_handler=handler,
        tool_registry=runner._tool_registry, tool_context=ctx,
    )

    events = [event async for event in agent.run_turn("Inspect the synthetic input.")]

    assert len(provider.tool_names_by_call) == 3
    assert "update_plan" not in provider.tool_names_by_call[0]
    assert "update_plan" in provider.tool_names_by_call[1]
    assert "update_plan" in provider.tool_names_by_call[2]
    assert updates == [(steps, explanation)]
    result = next(
        event for event in events
        if getattr(event, "kind", None) == "tool_result"
        and getattr(event, "tool_name", None) == "update_plan"
    )
    assert result.is_error is False
    assert json.loads(result.result)["status"] == "accepted"
    assert any(
        getattr(event, "kind", None) == "done"
        and getattr(event, "text", None) == "Synthetic task complete."
        for event in events
    )
