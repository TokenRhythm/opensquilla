from __future__ import annotations

import json

import pytest

from opensquilla.provider.types import ToolDefinition, ToolInputSchema
from opensquilla.tools.filter import filter_tools
from opensquilla.tools.registry import DEFAULT_MODEL_TOOL_NAMES, ToolRegistry
from opensquilla.tools.search import ToolSearchIndex, tokenize_for_bm25
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


def _definition(name: str, description: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema=ToolInputSchema(type="object", properties={}, required=[]),
    )


def test_bm25_tokenizer_normalizes_names_unicode_stop_words_and_stems() -> None:
    tokens = tokenize_for_bm25(
        "create_calendar_event Create a calendar event for connected users Café Gießen 🍕"
    )

    assert tokens[:6] == ("creat", "calendar", "event", "creat", "calendar", "event")
    assert "connect" in tokens
    assert "user" in tokens
    assert "cafe" in tokens
    assert "giessen" in tokens
    assert "pizza" in tokens
    assert "for" not in tokens


def test_bm25_index_matches_underscore_name_and_stemmed_description() -> None:
    index = ToolSearchIndex.from_definitions(
        [
            _definition(
                "create_calendar_event",
                "Create a calendar event for connected users",
            ),
            _definition("delete_email", "Permanently delete an email message"),
        ]
    )

    hits = index.search("create connected calendar events")

    assert hits
    assert hits[0].name == "create_calendar_event"


def test_exact_tool_name_is_always_the_first_match() -> None:
    index = ToolSearchIndex.from_definitions(
        [
            _definition("process", "Inspect and manage every background process"),
            _definition("background_process", "Inspect a process"),
        ]
    )

    assert index.search("background_process", limit=1)[0].name == "background_process"


def test_exact_stop_word_tool_name_remains_searchable() -> None:
    index = ToolSearchIndex.from_definitions([_definition("is", "Predicate helper")])

    assert index.search("is", limit=1)[0].name == "is"


def test_mcp_search_requires_an_explicit_namespace() -> None:
    index = ToolSearchIndex.from_definitions(
        [
            _definition("mcp__github__search", "Search GitHub repositories"),
            _definition("mcp__notion__search", "Search Notion pages"),
        ]
    )

    assert index.search("search repositories", namespace="builtin") == []
    assert [hit.name for hit in index.search("search", namespace="mcp__github")] == [
        "mcp__github__search"
    ]


def test_allow_deny_filter_gives_deny_precedence() -> None:
    tools = [_definition("read", "read"), _definition("write", "write")]

    filtered = filter_tools(tools, allow={"read", "write"}, deny={"write"})

    assert [tool.name for tool in filtered] == ["read"]


def test_model_surface_contains_default_authorized_mcp_and_disclosed_tools() -> None:
    registry = ToolRegistry()
    authorized = [
        _definition("exec_command", "Execute a command"),
        _definition("read_file", "Read a file"),
        ToolDefinition(
            name="tool_search",
            description="Search authorized tools",
            input_schema=ToolInputSchema(
                type="object",
                properties={"namespace": {"type": "string"}},
                required=[],
            ),
        ),
        _definition("agents_list", "List available agents"),
        _definition("mcp__github__search", "Search GitHub"),
    ]
    registry.register_mcp_namespace("mcp__github", "GitHub repository tools")
    ctx = ToolContext(is_owner=True, caller_kind=CallerKind.AGENT)

    initial = registry.to_model_tool_definitions(authorized, ctx)
    ctx.disclosed_tool_names.update({"agents_list", "mcp__github__search"})
    expanded = registry.to_model_tool_definitions(authorized, ctx)

    assert [tool.name for tool in initial] == [
        "exec_command",
        "read_file",
        "tool_search",
        "mcp__github__search",
    ]
    assert {tool.name for tool in expanded} == {
        "agents_list",
        "exec_command",
        "read_file",
        "tool_search",
        "mcp__github__search",
    }
    assert expanded[-1].name == "mcp__github__search"
    assert ctx.tool_search_namespaces == {"mcp__github": "GitHub repository tools"}
    search_definition = next(tool for tool in initial if tool.name == "tool_search")
    assert "mcp__github: GitHub repository tools" in search_definition.description
    assert search_definition.input_schema.properties["namespace"]["enum"] == [
        "builtin",
        "mcp__github",
    ]


def test_denied_tool_search_does_not_fail_open_to_full_catalog() -> None:
    async def _handler(**_kwargs: object) -> str:
        return "ok"

    from opensquilla.tools.types import ToolSpec

    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="tool_search", description="Search", parameters={}),
        _handler,
    )
    authorized = [
        _definition("exec_command", "Execute"),
        _definition("agents_list", "List agents"),
    ]

    model_surface = registry.to_model_tool_definitions(
        authorized,
        ToolContext(is_owner=True, caller_kind=CallerKind.AGENT),
    )

    assert [tool.name for tool in model_surface] == ["exec_command"]


def test_default_registry_projects_exact_stable_model_surface() -> None:
    import opensquilla.tools  # noqa: F401
    from opensquilla.tools.registry import get_default_registry

    registry = get_default_registry()
    ctx = ToolContext(is_owner=True, caller_kind=CallerKind.AGENT)
    authorized = registry.to_tool_definitions(ctx)

    model_surface = registry.to_model_tool_definitions(authorized, ctx)

    assert {tool.name for tool in model_surface} == DEFAULT_MODEL_TOOL_NAMES
    assert not any(tool.name.startswith("mcp__") for tool in model_surface)


@pytest.mark.asyncio
async def test_tool_search_discloses_matches_in_current_context() -> None:
    import opensquilla.tools  # noqa: F401
    from opensquilla.tools.registry import get_default_registry

    registry = get_default_registry()
    ctx = ToolContext(is_owner=True, caller_kind=CallerKind.AGENT)
    authorized = registry.to_tool_definitions(ctx)
    registry.to_model_tool_definitions(authorized, ctx)
    handler = registry.get("tool_search")
    assert handler is not None
    token = current_tool_context.set(ctx)
    try:
        payload = json.loads(await handler.handler(query="list directory files", limit=3))
    finally:
        current_tool_context.reset(token)

    assert payload["namespace"] == "builtin"
    assert payload["matches"]
    assert {item["name"] for item in payload["matches"]} <= ctx.disclosed_tool_names


@pytest.mark.asyncio
async def test_agent_adds_search_hits_to_next_provider_tool_surface() -> None:
    import opensquilla.tools  # noqa: F401
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import ToolCall
    from opensquilla.tools.dispatch import build_tool_handler
    from opensquilla.tools.registry import get_default_registry

    registry = get_default_registry()
    ctx = ToolContext(is_owner=True, caller_kind=CallerKind.AGENT)
    authorized = registry.to_tool_definitions(ctx)
    initial = registry.to_model_tool_definitions(authorized, ctx)
    agent = Agent(
        provider=object(),
        tool_definitions=initial,
        tool_handler=build_tool_handler(registry, ctx),
        tool_registry=registry,
        tool_context=ctx,
    )

    result = await agent._execute_tool(
        ToolCall(
            tool_use_id="search-1",
            tool_name="tool_search",
            arguments={"query": "list directory files", "limit": 3},
        )
    )

    assert result.is_error is False
    assert ctx.disclosed_tool_names
    assert ctx.disclosed_tool_names <= {tool.name for tool in agent.tool_definitions}


@pytest.mark.asyncio
async def test_full_agent_loop_searches_discloses_and_calls_real_tool(tmp_path) -> None:
    from collections.abc import AsyncIterator
    from typing import Any

    import opensquilla.tools  # noqa: F401
    from opensquilla.engine import Agent, AgentConfig
    from opensquilla.provider import (
        ChatConfig,
        DoneEvent,
        Message,
        TextDeltaEvent,
        ToolUseEndEvent,
        ToolUseStartEvent,
    )
    from opensquilla.tools.dispatch import build_tool_handler
    from opensquilla.tools.registry import get_default_registry

    class ProgressiveProvider:
        provider_name = "progressive-test"
        model = "progressive-test-model"

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
            if call_index == 0:
                yield ToolUseStartEvent(tool_use_id="search-1", tool_name="tool_search")
                yield ToolUseEndEvent(
                    tool_use_id="search-1",
                    tool_name="tool_search",
                    arguments={"query": "configured audio provider capabilities"},
                )
                yield DoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)
                return
            if call_index == 1:
                yield ToolUseStartEvent(
                    tool_use_id="capabilities-1",
                    tool_name="audio_provider_capabilities",
                )
                yield ToolUseEndEvent(
                    tool_use_id="capabilities-1",
                    tool_name="audio_provider_capabilities",
                    arguments={},
                )
                yield DoneEvent(stop_reason="tool_use", input_tokens=1, output_tokens=1)
                return
            yield TextDeltaEvent(text="done")
            yield DoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)

        async def list_models(self) -> list[Any]:
            return []

    registry = get_default_registry()
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        workspace_dir=str(tmp_path),
    )
    authorized = registry.to_tool_definitions(ctx)
    initial = registry.to_model_tool_definitions(authorized, ctx)
    provider = ProgressiveProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=3,
            workspace_dir=str(tmp_path),
            model_id=provider.model,
        ),
        tool_definitions=initial,
        tool_handler=build_tool_handler(registry, ctx),
        tool_registry=registry,
        tool_context=ctx,
    )

    events = [event async for event in agent.run_turn("inspect the directory")]

    assert "audio_provider_capabilities" not in provider.tool_names_by_call[0]
    assert "audio_provider_capabilities" in provider.tool_names_by_call[1]
    assert "audio_provider_capabilities" in provider.tool_names_by_call[2]
    capabilities_result = next(
        event
        for event in events
        if getattr(event, "kind", None) == "tool_result"
        and getattr(event, "tool_name", None) == "audio_provider_capabilities"
    )
    assert capabilities_result.is_error is False
    assert any(
        getattr(event, "kind", None) == "done" and getattr(event, "text", None) == "done"
        for event in events
    )
