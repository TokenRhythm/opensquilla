"""Tests for the ``update_plan`` builtin planning tool.

``update_plan`` ships with ``exposed_by_default=False``: the default tool
surface must stay byte-identical, and deployments opt in via ``[tools]
also_allow`` (profile path) or ``allowed_tools``/``surfaced_tools`` overrides.
Each call replaces the whole plan, stores the latest plan per session key,
returns a compact checklist summary, and emits an ``update_plan.updated``
runtime event for experiment attribution.
"""

from __future__ import annotations

import json

import pytest

from opensquilla.engine.types import ToolCall
from opensquilla.tools.builtin import planning
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.policy_helpers import ToolPolicy, apply_tool_policy
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import (
    CallerKind,
    ToolContext,
    ToolError,
    current_tool_context,
)


def _ctx(
    *,
    session_key: str = "agent:main:test",
    allowed: bool = False,
    on_runtime_event=None,
) -> ToolContext:
    return ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        session_key=session_key,
        agent_id="main",
        allowed_tools={"update_plan"} if allowed else None,
        on_runtime_event=on_runtime_event,
    )


async def _call(ctx: ToolContext, **kwargs) -> str:
    token = current_tool_context.set(ctx)
    try:
        return await planning.update_plan(**kwargs)
    finally:
        current_tool_context.reset(token)


def _tool_names(ctx: ToolContext) -> set[str]:
    return {tool.name for tool in get_default_registry().to_tool_definitions(ctx)}


def test_update_plan_is_hidden_on_default_surfaces() -> None:
    registry = get_default_registry()
    assert registry.get("update_plan") is not None

    assert "update_plan" not in _tool_names(
        ToolContext(is_owner=True, caller_kind=CallerKind.AGENT)
    )
    assert "update_plan" not in _tool_names(
        ToolContext(is_owner=False, caller_kind=CallerKind.CHANNEL)
    )
    assert "update_plan" not in _tool_names(
        ToolContext(is_owner=True, caller_kind=CallerKind.SUBAGENT)
    )


def test_update_plan_is_visible_when_explicitly_allowed_or_surfaced() -> None:
    assert _tool_names(_ctx(allowed=True)) == {"update_plan"}
    assert "update_plan" in _tool_names(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.AGENT,
            surfaced_tools={"update_plan"},
        )
    )


def test_update_plan_profile_also_allow_opts_the_tool_in() -> None:
    registry = get_default_registry()
    base = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(profile="repo_coding_source_edit_balanced"),
    )
    assert "update_plan" not in _tool_names(base)

    opted_in = apply_tool_policy(
        ToolContext(is_owner=True),
        available_tools=registry.list_names(),
        agent_policy=ToolPolicy(
            profile="repo_coding_source_edit_balanced",
            also_allow=frozenset({"update_plan"}),
        ),
    )
    assert "update_plan" in _tool_names(opted_in)


@pytest.mark.asyncio
async def test_update_plan_returns_summary_counts_and_checklist() -> None:
    result = await _call(
        _ctx(session_key="agent:main:summary"),
        plan=[
            {"step": "Survey the inputs", "status": "completed"},
            {"step": "Draft the change", "status": "in_progress"},
            {"step": "Check the result", "status": "pending"},
        ],
    )

    lines = result.splitlines()
    assert lines[0] == "Plan updated (3 steps: 1 completed, 1 in_progress, 1 pending)"
    assert lines[1] == "[x] Survey the inputs"
    assert lines[2] == "[>] Draft the change"
    assert lines[3] == "[ ] Check the result"
    assert "warning" not in result


@pytest.mark.asyncio
async def test_update_plan_rejects_invalid_plans() -> None:
    ctx = _ctx(session_key="agent:main:invalid")

    with pytest.raises(ToolError):
        await _call(ctx, plan=[])
    with pytest.raises(ToolError):
        await _call(ctx, plan=[{"step": "Do the work", "status": "done"}])
    with pytest.raises(ToolError):
        await _call(
            ctx,
            plan=[{"step": f"Step {i}", "status": "pending"} for i in range(65)],
        )
    with pytest.raises(ToolError):
        await _call(ctx, plan=[{"step": "   ", "status": "pending"}])
    with pytest.raises(ToolError):
        await _call(ctx, plan=[{"step": "x" * 513, "status": "pending"}])

    assert planning.latest_plan("agent:main:invalid") is None


@pytest.mark.asyncio
async def test_update_plan_accepts_multiple_in_progress_with_warning() -> None:
    result = await _call(
        _ctx(session_key="agent:main:conflict"),
        plan=[
            {"step": "First track", "status": "in_progress"},
            {"step": "Second track", "status": "in_progress"},
        ],
    )

    assert result.splitlines()[0] == (
        "Plan updated (2 steps: 0 completed, 2 in_progress, 0 pending)"
    )
    assert "warning: 2 steps are in_progress" in result
    assert "keep at most one step in_progress" in result


@pytest.mark.asyncio
async def test_update_plan_replaces_stored_plan_per_session() -> None:
    ctx = _ctx(session_key="agent:main:replace")

    await _call(
        ctx,
        plan=[{"step": "Original step", "status": "pending"}],
        explanation="initial plan",
    )
    stored = planning.latest_plan("agent:main:replace")
    assert stored is not None
    assert stored["steps"] == [{"step": "Original step", "status": "pending"}]
    assert stored["explanation"] == "initial plan"
    assert stored["revision"] == 1

    await _call(
        ctx,
        plan=[
            {"step": "Original step", "status": "completed"},
            {"step": "Follow-up step", "status": "in_progress"},
        ],
    )
    stored = planning.latest_plan("agent:main:replace")
    assert stored is not None
    assert stored["steps"] == [
        {"step": "Original step", "status": "completed"},
        {"step": "Follow-up step", "status": "in_progress"},
    ]
    assert stored["explanation"] is None
    assert stored["revision"] == 2


@pytest.mark.asyncio
async def test_update_plan_emits_runtime_event_with_counts() -> None:
    events: list[dict] = []
    await _call(
        _ctx(session_key="agent:main:event", on_runtime_event=events.append),
        plan=[
            {"step": "Completed step", "status": "completed"},
            {"step": "Active step", "status": "in_progress"},
            {"step": "Queued step", "status": "pending"},
            {"step": "Second queued step", "status": "pending"},
        ],
    )

    assert len(events) == 1
    event = events[0]
    assert event["feature"] == "update_plan"
    assert event["name"] == "update_plan.updated"
    assert event["action"] == "replace_plan"
    assert event["tool"] == "update_plan"
    assert event["step_count"] == 4
    assert event["completed"] == 1
    assert event["in_progress"] == 1
    assert event["pending"] == 2
    assert event["session_key"] == "agent:main:event"
    assert event["agent_id"] == "main"


@pytest.mark.asyncio
async def test_update_plan_without_event_callback_emits_nothing_and_still_works() -> None:
    result = await _call(
        _ctx(session_key="agent:main:no-callback"),
        plan=[{"step": "Only step", "status": "pending"}],
    )

    assert result.splitlines()[0] == (
        "Plan updated (1 step: 0 completed, 0 in_progress, 1 pending)"
    )


@pytest.mark.asyncio
async def test_update_plan_dispatch_happy_path() -> None:
    handler = build_tool_handler(
        get_default_registry(), _ctx(session_key="agent:main:dispatch", allowed=True)
    )

    result = await handler(
        ToolCall(
            tool_use_id="call-plan",
            tool_name="update_plan",
            arguments={
                "plan": [
                    {"step": "Land the change", "status": "in_progress"},
                    {"step": "Confirm behavior", "status": "pending"},
                ]
            },
        )
    )

    assert result.is_error is False
    assert "Plan updated (2 steps" in result.content
    assert "[>] Land the change" in result.content


@pytest.mark.asyncio
async def test_update_plan_dispatch_rejects_bad_status_via_schema() -> None:
    handler = build_tool_handler(
        get_default_registry(),
        _ctx(session_key="agent:main:dispatch-bad", allowed=True),
    )

    result = await handler(
        ToolCall(
            tool_use_id="call-plan-bad",
            tool_name="update_plan",
            arguments={"plan": [{"step": "Do the work", "status": "started"}]},
        )
    )

    assert result.is_error is True
    payload = json.loads(result.content)
    assert payload["reason_code"] == "schema_validation_failed"
    assert planning.latest_plan("agent:main:dispatch-bad") is None


@pytest.mark.asyncio
async def test_update_plan_dispatch_denied_when_allow_list_excludes_it() -> None:
    # exposed_by_default=False gates listing only (engine-wide hidden-tool
    # contract), so policy-restricted surfaces rely on the dispatch chain:
    # a non-None allowed_tools that omits update_plan must deny by name.
    handler = build_tool_handler(
        get_default_registry(),
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.AGENT,
            session_key="agent:main:dispatch-denied",
            agent_id="main",
            allowed_tools={"session_status"},
        ),
    )

    result = await handler(
        ToolCall(
            tool_use_id="call-plan-denied",
            tool_name="update_plan",
            arguments={"plan": [{"step": "Should not run", "status": "pending"}]},
        )
    )

    assert result.is_error is True
    assert planning.latest_plan("agent:main:dispatch-denied") is None


@pytest.mark.asyncio
async def test_update_plan_dispatch_denied_for_channel_caller() -> None:
    handler = build_tool_handler(
        get_default_registry(),
        ToolContext(
            is_owner=False,
            caller_kind=CallerKind.CHANNEL,
            session_key="channel:guest:dispatch",
            agent_id="main",
        ),
    )

    result = await handler(
        ToolCall(
            tool_use_id="call-plan-channel",
            tool_name="update_plan",
            arguments={"plan": [{"step": "Should not run", "status": "pending"}]},
        )
    )

    assert result.is_error is True
    assert planning.latest_plan("channel:guest:dispatch") is None
