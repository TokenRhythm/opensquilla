from __future__ import annotations

from unittest.mock import patch

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig, SquillaRouterConfig
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import CallerKind, ToolContext, ToolSpec


async def _handler(**_kwargs: object) -> str:
    return "ok"


def _runner() -> TurnRunner:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="read_file",
            description="Read a file",
            parameters={
                "path": {"type": "string", "examples": ["original.txt"]},
                "internal_token": {"type": "string"},
            },
            required=["path", "internal_token"],
            runtime_only_arguments=frozenset({"internal_token"}),
        ),
        _handler,
    )
    registry.register(
        ToolSpec(name="owner_only", description="Owner tool", parameters={}, owner_only=True),
        _handler,
    )
    registry.register(
        ToolSpec(
            name="router_control", description="Router control",
            parameters={"target_id": {"type": "string"}},
        ),
        _handler,
    )
    return TurnRunner(provider_selector=None, config=GatewayConfig(), tool_registry=registry)


@pytest.mark.parametrize("authority", ["none", "owner", "guest"])
def test_build_tools_materializes_catalog_once(authority: str) -> None:
    runner = _runner()
    context = None if authority == "none" else ToolContext(
        is_owner=authority == "owner",
        guest_safe=authority == "guest",
        caller_kind=CallerKind.WEB,
    )
    registry = runner._tool_registry
    with patch.object(registry, "to_tool_definitions", wraps=registry.to_tool_definitions) as build:
        definitions, _handler_fn = runner._build_tools(context)

    assert definitions
    assert build.call_count == 1


def test_build_tools_keeps_authority_and_schema_isolated_between_calls() -> None:
    runner = _runner()
    owner = ToolContext(is_owner=True, caller_kind=CallerKind.WEB)
    owner_defs, _ = runner._build_tools(owner)
    owner_by_name = {definition.name: definition for definition in owner_defs}
    assert "owner_only" in owner_by_name
    owner_schema = owner_by_name["read_file"].input_schema
    assert "internal_token" not in owner_schema.properties
    assert owner_schema.required == ["path"]
    owner_schema.properties["path"]["examples"].append("owner-only.txt")

    guest = ToolContext(
        is_owner=False, guest_safe=True, caller_kind=CallerKind.WEB,
        allowed_tools={"read_file", "owner_only", "router_control"},
    )
    guest_defs, _ = runner._build_tools(guest)
    assert [definition.name for definition in guest_defs] == ["read_file"]
    assert guest_defs[0].input_schema.properties["path"]["examples"] == ["original.txt"]
    assert "owner_only" not in guest.authorized_tool_names

    denied_defs, _ = runner._build_tools(ToolContext(
        is_owner=True, caller_kind=CallerKind.WEB, denied_tools={"read_file"},
    ))
    assert "read_file" not in {definition.name for definition in denied_defs}
    registered = runner._tool_registry.get("read_file").spec
    assert registered.parameters["path"]["examples"] == ["original.txt"]
    assert "internal_token" in registered.parameters


def test_build_tools_keeps_router_schema_specific_to_each_context() -> None:
    runner = _runner()

    def build(tier: str):
        definitions, _ = runner._build_tools(ToolContext(
            is_owner=True,
            caller_kind=CallerKind.WEB,
            router_control_config=SquillaRouterConfig(
                enabled=True, tiers={tier: {"model": "test-model", "provider": "test"}},
            ),
        ))
        return next(definition for definition in definitions if definition.name == "router_control")

    first = build("c1")
    second = build("c2")

    assert first.input_schema.properties["target_id"]["enum"] == ["tier:c1"]
    assert second.input_schema.properties["target_id"]["enum"] == ["tier:c2"]
    assert "enum" not in runner._tool_registry.get("router_control").spec.parameters["target_id"]
