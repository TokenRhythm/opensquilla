from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio

from opensquilla.mcp.client import MCPClient
from opensquilla.mcp.discovery import mcp_namespace, mcp_tool_name
from opensquilla.mcp.types import MCPServerConfig, MCPToolDef, MCPToolResult
from opensquilla.tools.registry import ToolRegistry


class FakeMCPClient(MCPClient):
    def __init__(
        self,
        config: MCPServerConfig,
        tools: list[MCPToolDef] | None = None,
        *,
        fail_list: bool = False,
        call_result: MCPToolResult | None = None,
    ) -> None:
        super().__init__(config)
        self.tools = tools or []
        self.fail_list = fail_list
        self.call_result = call_result
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def list_tools(self) -> list[MCPToolDef]:
        if self.fail_list:
            raise RuntimeError("list failed")
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        if self.call_result is not None:
            return self.call_result
        return MCPToolResult(content=f"{name}:{arguments}")


def test_mcp_callable_names_are_provider_safe_bounded_and_namespaced() -> None:
    namespace = mcp_namespace(
        "Very Long Café MCP Server Name With Spaces And More Characters Than Allowed"
    )
    first = mcp_tool_name(namespace, "search.connected/services" * 8)
    second = mcp_tool_name(namespace, "search connected services" * 8)

    assert len(namespace) <= 32
    assert len(first) <= 64
    assert first.startswith(f"{namespace}__")
    assert first.replace("_", "").replace("-", "").isalnum()
    assert first != second
    assert mcp_tool_name("mcp__case", "Search") != mcp_tool_name("mcp__case", "search")
    assert mcp_tool_name("mcp__unicode", "café") != mcp_tool_name("mcp__unicode", "cafe")


@pytest_asyncio.fixture(autouse=True)
async def _close_mcp_clients():
    from opensquilla.mcp.discovery import close_active_clients

    await close_active_clients()
    yield
    await close_active_clients()


@pytest.mark.asyncio
async def test_discovered_mcp_clients_have_owner_and_close_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.mcp import discovery

    config = MCPServerConfig(
        name="docs",
        description="Documentation search",
        transport="stdio",
        command="mock-mcp",
    )
    client = FakeMCPClient(
        config,
        tools=[
            MCPToolDef(
                name="lookup",
                description="Lookup docs",
                input_schema={"properties": {"q": {"type": "string"}}, "required": ["q"]},
            )
        ],
    )
    monkeypatch.setattr(discovery, "create_client", lambda _config: client)

    registry = ToolRegistry()
    names = await discovery.discover_and_register(config, registry, owner="gateway")
    snapshot = discovery.active_clients_snapshot()

    assert names == ["mcp__docs__lookup"]
    assert len(snapshot) == 1
    assert snapshot[0].owner == "gateway"
    assert snapshot[0].server_name == "docs"
    assert snapshot[0].transport == "stdio"
    assert snapshot[0].client is client
    assert registry.mcp_namespaces() == {"mcp__docs": "Documentation search"}
    assert registry.get("mcp__docs__lookup") is not None

    from opensquilla.provider.anthropic import _build_tool_payload
    from opensquilla.provider.ollama import _build_ollama_tool
    from opensquilla.provider.openai import _build_openai_tool
    from opensquilla.provider.openai_codex import _codex_tool
    from opensquilla.provider.openai_responses import _responses_tool
    from opensquilla.tools.types import CallerKind, ToolContext

    definition = next(
        tool
        for tool in registry.to_tool_definitions(
            ToolContext(is_owner=True, caller_kind=CallerKind.AGENT)
        )
        if tool.name == "mcp__docs__lookup"
    )
    assert _build_openai_tool(definition)["function"]["name"] == definition.name
    assert _build_tool_payload(definition)["name"] == definition.name
    assert _build_ollama_tool(definition)["function"]["name"] == definition.name
    assert _responses_tool(definition)["name"] == definition.name
    assert _codex_tool(definition)["name"] == definition.name

    assert await discovery.close_active_clients(owner="docs") == 1
    assert client.closed is True
    assert discovery.active_clients_snapshot() == ()
    assert registry.mcp_namespaces() == {}
    assert registry.get("mcp__docs__lookup") is None


@pytest.mark.asyncio
async def test_colliding_mcp_namespaces_fail_without_damaging_first_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.mcp import discovery

    first_config = MCPServerConfig(
        name="Git Hub",
        transport="stdio",
        command="first-mcp",
    )
    second_config = MCPServerConfig(
        name="git-hub",
        transport="stdio",
        command="second-mcp",
    )
    first = FakeMCPClient(
        first_config,
        tools=[MCPToolDef(name="search", description="Search", input_schema={})],
    )
    second = FakeMCPClient(second_config)
    clients = iter((first, second))
    monkeypatch.setattr(discovery, "create_client", lambda _config: next(clients))
    registry = ToolRegistry()

    await discovery.discover_and_register(first_config, registry)
    with pytest.raises(ValueError, match="already registered"):
        await discovery.discover_and_register(second_config, registry)

    assert second.closed is True
    assert registry.get("mcp__git-hub__search") is not None
    assert registry.mcp_namespaces() == {
        "mcp__git-hub": "Tools provided by the Git Hub MCP server."
    }
    assert len(discovery.active_clients_snapshot()) == 1


@pytest.mark.asyncio
async def test_failed_mcp_discovery_closes_client_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.mcp import discovery

    config = MCPServerConfig(name="broken", transport="stdio", command="mock-mcp")
    client = FakeMCPClient(config, fail_list=True)
    monkeypatch.setattr(discovery, "create_client", lambda _config: client)

    with pytest.raises(RuntimeError, match="list failed"):
        await discovery.discover_and_register(config, ToolRegistry())

    assert client.closed is True
    assert discovery.active_clients_snapshot() == ()


@pytest.mark.asyncio
async def test_cancelled_mcp_discovery_closes_client_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.mcp import discovery

    config = MCPServerConfig(name="slow", transport="stdio", command="mock-mcp")
    client = FakeMCPClient(config)
    listing_started = asyncio.Event()
    never_finish = asyncio.Event()

    async def list_tools() -> list[MCPToolDef]:
        listing_started.set()
        await never_finish.wait()
        return []

    monkeypatch.setattr(client, "list_tools", list_tools)
    monkeypatch.setattr(discovery, "create_client", lambda _config: client)
    task = asyncio.create_task(discovery.discover_and_register(config, ToolRegistry()))
    await listing_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.closed is True
    assert discovery.active_clients_snapshot() == ()


@pytest.mark.asyncio
async def test_malformed_optional_mcp_schema_fields_degrade_to_empty_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.mcp import discovery
    from opensquilla.tools.types import CallerKind, ToolContext

    config = MCPServerConfig(name="loose", transport="stdio", command="mock-mcp")
    client = FakeMCPClient(
        config,
        tools=[
            MCPToolDef(
                name="lookup",
                description="Lookup",
                input_schema={"properties": None, "required": "q"},
            )
        ],
    )
    monkeypatch.setattr(discovery, "create_client", lambda _config: client)
    registry = ToolRegistry()

    await discovery.discover_and_register(config, registry)
    definitions = registry.to_tool_definitions(
        ToolContext(is_owner=True, caller_kind=CallerKind.AGENT)
    )
    definition = next(tool for tool in definitions if tool.name == "mcp__loose__lookup")

    assert definition.input_schema.properties == {}
    assert definition.input_schema.required == []


@pytest.mark.asyncio
async def test_registered_handler_surfaces_client_error_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.mcp import discovery
    from opensquilla.tool_boundary import ToolCall
    from opensquilla.tools.dispatch import build_tool_handler

    config = MCPServerConfig(name="docs", transport="stdio", command="mock-mcp")
    client = FakeMCPClient(
        config,
        tools=[MCPToolDef(name="lookup", description="Lookup docs", input_schema={})],
        call_result=MCPToolResult(content="invalid params", is_error=True),
    )
    monkeypatch.setattr(discovery, "create_client", lambda _config: client)

    registry = ToolRegistry()
    await discovery.discover_and_register(config, registry)
    handler = build_tool_handler(registry)
    result = await handler(ToolCall(tool_use_id="tu1", tool_name="mcp__docs__lookup", arguments={}))

    assert result.is_error is True
    assert "invalid params" in result.content
