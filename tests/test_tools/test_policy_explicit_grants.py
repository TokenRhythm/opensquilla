"""Explicit config grants must not narrow or broaden the default tool catalog."""

from __future__ import annotations

import json

import pytest

from opensquilla.tools.builtin.tool_search import tool_search
from opensquilla.tools.policy_helpers import (
    apply_tool_policy_from_config,
    apply_tool_policy_layer,
)
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext, ToolSpec, current_tool_context


async def _handler() -> str:
    return "ok"


def registry() -> ToolRegistry:
    tools = ToolRegistry()
    for name, access in (
        ("tool_search", "allow"), ("read_file", "allow"),
        ("read_source", "deny"), ("edit_source", "deny"),
    ):
        tools.register(ToolSpec(
            name=name, description=f"{name} tool", parameters={}, default_access=access,
        ), _handler)
    return tools


@pytest.mark.parametrize("profile", [None, "full"])
@pytest.mark.parametrize("field", ["allow", "also_allow"])
async def test_unrestricted_profile_preserves_explicit_default_deny_grant(profile, field):
    tools = registry()
    context = apply_tool_policy_from_config(
        ToolContext(is_owner=True), available_tools=tools.list_names(),
        config={"tools": {"profile": profile, field: ["read_source"]}},
    )
    assert context.allowed_tools is None
    authorized = tools.to_tool_definitions(context)
    assert {tool.name for tool in authorized} == {"tool_search", "read_file", "read_source"}
    initial = tools.to_model_tool_definitions(authorized, context)
    assert "read_source" not in {tool.name for tool in initial}
    token = current_tool_context.set(context)
    try:
        result = json.loads(await tool_search(query="read_source"))
    finally:
        current_tool_context.reset(token)
    assert "read_source" in {hit["name"] for hit in result["matches"]}
    assert "edit_source" not in context.authorized_tool_names
    disclosed = tools.to_model_tool_definitions(authorized, context)
    assert "read_source" in {tool.name for tool in disclosed}


def test_plain_full_profile_does_not_grant_default_deny_tools():
    tools = registry()
    context = apply_tool_policy_from_config(
        ToolContext(is_owner=True), available_tools=tools.list_names(),
        config={"tools": {"profile": "full"}},
    )
    assert {tool.name for tool in tools.to_tool_definitions(context)} == {
        "tool_search", "read_file",
    }


@pytest.mark.parametrize("restriction", ["deny", "agent-profile", "owner", "guest"])
def test_explicit_grants_keep_authorization_boundaries(restriction):
    tools = registry()
    config = {"tools": {"allow": ["read_source"]}}
    context = ToolContext(is_owner=True)
    target = "read_source"
    if restriction == "deny":
        config["tools"]["deny"] = ["read_source"]
    elif restriction == "agent-profile":
        config["agents"] = {"main": {"tools": {"profile": "minimal"}}}
    elif restriction == "owner":
        tools.get("read_source").spec.owner_only = True
        context.is_owner = False
    elif restriction == "guest":
        context.guest_safe = True
        target = "host_only"
        tools.register(ToolSpec(
            name=target, description="Host tool", parameters={}, default_access="deny",
        ), _handler)
        config["tools"]["allow"] = [target]
    context = apply_tool_policy_from_config(
        context, available_tools=tools.list_names(), config=config,
    )
    assert target not in {tool.name for tool in tools.to_tool_definitions(context)}


def test_standalone_policy_layer_preserves_grants_and_hard_denies():
    tools = registry()
    context = apply_tool_policy_layer(
        ToolContext(is_owner=True), {"also_allow": ["read_source", "edit_source"]},
        available_tools=tools.list_names(), hard_denied={"edit_source"},
    )
    assert context.allowed_tools is None
    assert {tool.name for tool in tools.to_tool_definitions(context)} == {
        "tool_search", "read_file", "read_source",
    }


def test_actual_source_tool_is_searchable_after_explicit_grant():
    import opensquilla.tools.builtin  # noqa: F401
    from opensquilla.tools.registry import get_default_registry

    tools = get_default_registry()
    context = apply_tool_policy_from_config(
        ToolContext(is_owner=True), available_tools=tools.list_names(),
        config={"tools": {"allow": ["read_source"]}},
    )
    authorized = tools.to_tool_definitions(context)
    names = {tool.name for tool in authorized}
    assert {"read_file", "read_source", "publish_artifact"} <= names
    assert "edit_source" not in names
    tools.to_model_tool_definitions(authorized, context)
    assert "read_source" in {
        hit.name for hit in context.tool_search_index.search("read_source", namespace="builtin")
    }


async def test_runtime_carries_explicit_grant_into_next_agent_step():
    import opensquilla.tools.builtin  # noqa: F401
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.runtime import TurnRunner
    from opensquilla.engine.types import ToolCall
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.tools.registry import get_default_registry

    tools = get_default_registry()
    context = ToolContext(is_owner=True)
    runner = TurnRunner(
        provider_selector=None, tool_registry=tools,
        config=GatewayConfig(tools={"allow": ["read_source"]}),
    )
    initial, handler = runner._build_tools(context)
    assert "read_source" not in {tool.name for tool in initial}
    agent = Agent(
        provider=object(), tool_definitions=initial, tool_handler=handler,
        tool_registry=tools, tool_context=context,
    )
    result = await agent._execute_tool(ToolCall(
        tool_use_id="discover-source", tool_name="tool_search",
        arguments={"query": "read_source", "limit": 1},
    ))
    assert result.is_error is False
    assert "read_source" in context.authorized_tool_names
    assert "read_source" in context.disclosed_tool_names
    assert "read_source" in {tool.name for tool in agent.tool_definitions}
    assert "edit_source" not in {tool.name for tool in agent.tool_definitions}
