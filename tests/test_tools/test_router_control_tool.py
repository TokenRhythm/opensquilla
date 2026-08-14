from __future__ import annotations

import json

import pytest

from opensquilla.gateway.config import SquillaRouterConfig, _router_tier_profile_defaults
from opensquilla.router_control import RouterControlHoldStore
from opensquilla.tool_boundary import ToolCall
from opensquilla.tools import get_default_registry
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.types import CallerKind, ToolContext


def _ctx(*, replay_depth: int = 0, hold_applied: bool = False) -> ToolContext:
    return ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        session_key="agent:main:test-router-control-tool",
        router_control_config=SquillaRouterConfig(
            enabled=True,
            rollout_phase="full",
            require_router_runtime=False,
            tiers=_router_tier_profile_defaults("openrouter"),
        ),
        router_control_hold_store=RouterControlHoldStore(),
        router_control_replay_depth=replay_depth,
        router_control_turn_hold_applied=hold_applied,
    )


@pytest.mark.asyncio
async def test_router_control_set_hold_writes_store_and_requests_replay() -> None:
    ctx = _ctx()
    handler = build_tool_handler(get_default_registry(), ctx)

    result = await handler(
        ToolCall(
            tool_use_id="call-1",
            tool_name="router_control",
            arguments={
                "action": "set_hold",
                "target_id": "tier:c3",
                "evidence": "use c3",
            },
        )
    )

    payload = json.loads(result.content)
    assert result.is_error is False
    assert result.terminates_turn is True
    assert payload["accepted"] is True
    assert payload["target_tier"] == "c3"
    assert payload["target_model"] == "anthropic/claude-opus-4.8"
    assert payload["target_label"] == "claude-opus-4.8"
    assert payload["target_execution_kind"] == "single_model"
    assert payload["target_model_role"] == "selected_model"
    assert payload["replay_required"] is True
    hold = ctx.router_control_hold_store.get_valid(ctx.session_key or "")
    assert hold is not None
    assert hold.tier == "c3"


@pytest.mark.asyncio
async def test_router_control_ensemble_target_keeps_anchor_but_reports_fusion() -> None:
    ctx = _ctx()
    ctx.router_control_config.tiers["c3"]["ensemble_enabled"] = True
    handler = build_tool_handler(get_default_registry(), ctx)

    result = await handler(
        ToolCall(
            tool_use_id="call-ensemble",
            tool_name="router_control",
            arguments={
                "action": "set_hold",
                "target_id": "tier:c3",
                "evidence": "use the highest tier",
            },
        )
    )

    payload = json.loads(result.content)
    assert payload["target_model"] == "anthropic/claude-opus-4.8"
    assert payload["target_provider"] == "openrouter"
    assert payload["target_label"] == "multi-model fusion"
    assert payload["target_execution_kind"] == "ensemble"
    assert payload["target_model_role"] == "ensemble_anchor"
    hold = ctx.router_control_hold_store.get_valid(ctx.session_key or "")
    assert hold is not None
    assert hold.model == "anthropic/claude-opus-4.8"
    assert hold.provider == "openrouter"


@pytest.mark.asyncio
async def test_router_control_rejects_alias_without_mutating_hold() -> None:
    ctx = _ctx()
    handler = build_tool_handler(get_default_registry(), ctx)

    result = await handler(
        ToolCall(
            tool_use_id="call-1",
            tool_name="router_control",
            arguments={
                "action": "set_hold",
                "target_id": "Claude Opus 4.8",
                "evidence": "use Claude Opus 4.8",
            },
        )
    )

    payload = json.loads(result.content)
    assert result.is_error is False
    assert result.terminates_turn is False
    assert payload["accepted"] is False
    assert payload["replay_required"] is False
    assert ctx.router_control_hold_store.get_valid(ctx.session_key or "") is None


def test_router_control_tool_schema_includes_dynamic_target_enum() -> None:
    ctx = _ctx()
    definitions = get_default_registry().to_tool_definitions(ctx)
    router_tool = next(tool for tool in definitions if tool.name == "router_control")

    target_schema = router_tool.input_schema.properties["target_id"]

    assert "enum" in target_schema
    assert "tier:c3" in target_schema["enum"]
    assert "tier:t3" not in target_schema["enum"]
    assert "model:z-ai/glm-5.2" not in target_schema["enum"]


@pytest.mark.asyncio
async def test_router_control_clear_hold_replays_when_hold_already_selected_turn() -> None:
    ctx = _ctx(hold_applied=True)
    target = next(
        target
        for target in ctx.router_control_hold_store.build_targets(ctx.router_control_config)
        if target.target_id == "tier:c3"
    )
    ctx.router_control_hold_store.set_hold(ctx.session_key or "", target, evidence="use c3")
    handler = build_tool_handler(get_default_registry(), ctx)

    result = await handler(
        ToolCall(
            tool_use_id="call-1",
            tool_name="router_control",
            arguments={
                "action": "clear_hold",
                "evidence": "back to automatic routing",
            },
        )
    )

    payload = json.loads(result.content)
    assert result.terminates_turn is True
    assert payload["accepted"] is True
    assert payload["action"] == "clear_hold"
    assert payload["replay_required"] is True
    assert ctx.router_control_hold_store.get_valid(ctx.session_key or "") is None
