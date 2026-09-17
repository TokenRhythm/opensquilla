from __future__ import annotations

import json
import os

import pytest

from opensquilla.engine.types import ToolCall
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import CallerKind, ToolContext, ToolSpec

GUEST_ALLOWED = {
    "read_file",
    "read_source",
    "read_spreadsheet",
    "write_file",
    "create_source",
    "edit_file",
    "edit_source",
    "list_dir",
    "glob_search",
    "source_symbols",
    "grep_search",
    "apply_patch",
    "http_request",
    "web_fetch",
    "web_search",
    "web_discover",
}

GUEST_DENIED = {
    "sessions_list",
    "sessions_history",
    "sessions_send",
    "sessions_spawn",
    "sessions_yield",
    "session_status",
    "agents_list",
    "subagents",
    "skill_list",
    "skill_view",
    "skill_create",
    "skill_edit",
    "skill_delete",
    "memory_search",
    "memory_save",
    "memory_get",
    "memory_delete",
    "gateway",
    "cron",
    "audio_config",
    "approval_request",
    "setup",
    "host_state",
    "exec_command",
    "unknown_future_tool",
}


def _registry(names: set[str], calls: list[str]) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:

        async def handler(_name: str = name, **_kwargs: object) -> str:
            calls.append(_name)
            return _name

        registry.register(
            ToolSpec(name=name, description=name, parameters={}, required=[]),
            handler,
        )
    return registry


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_state", ["guest", "invalid"])
async def test_missing_and_invalid_guest_dispatch_default_denies_unreviewed_tools(
    auth_state: str,
) -> None:
    calls: list[str] = []
    registry = _registry(GUEST_ALLOWED | GUEST_DENIED, calls)
    ctx = ToolContext(
        is_owner=False,
        caller_kind=CallerKind.WEB,
        guest_safe=True,
        session_key=f"agent:main:webchat:{auth_state}",
    )
    handler = build_tool_handler(registry, ctx)

    for name in sorted(GUEST_DENIED):
        result = await handler(ToolCall(tool_use_id=f"guest-{name}", tool_name=name, arguments={}))
        assert result.is_error is True, name
        assert json.loads(result.content)["error_class"] == "PolicyDenied"

    assert calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows process denial regression")
@pytest.mark.asyncio
async def test_windows_guest_exec_command_handler_never_runs() -> None:
    calls: list[str] = []
    registry = _registry({"exec_command"}, calls)
    handler = build_tool_handler(
        registry,
        ToolContext(is_owner=False, caller_kind=CallerKind.WEB, guest_safe=True),
    )

    result = await handler(
        ToolCall(tool_use_id="guest-windows-exec", tool_name="exec_command", arguments={})
    )

    assert result.is_error is True
    assert calls == []


@pytest.mark.asyncio
async def test_guest_dispatch_allows_only_reviewed_file_and_network_tools() -> None:
    calls: list[str] = []
    registry = _registry(GUEST_ALLOWED | GUEST_DENIED, calls)
    ctx = ToolContext(is_owner=False, caller_kind=CallerKind.WEB, guest_safe=True)

    visible = {tool.name for tool in registry.to_tool_definitions(ctx)}
    assert visible == GUEST_ALLOWED

    handler = build_tool_handler(registry, ctx)
    for name in ("read_file", "write_file", "web_fetch", "http_request"):
        result = await handler(ToolCall(tool_use_id=f"guest-{name}", tool_name=name, arguments={}))
        assert result.is_error is False
    assert calls == ["read_file", "write_file", "web_fetch", "http_request"]


@pytest.mark.asyncio
async def test_guest_explicit_allowlist_is_intersected_with_hard_allowlist() -> None:
    calls: list[str] = []
    registry = _registry({"read_file", "sessions_send"}, calls)
    ctx = ToolContext(
        guest_safe=True,
        allowed_tools={"read_file", "sessions_send"},
    )

    assert {tool.name for tool in registry.to_tool_definitions(ctx)} == {"read_file"}
    handler = build_tool_handler(registry, ctx)
    denied = await handler(
        ToolCall(tool_use_id="guest-send", tool_name="sessions_send", arguments={})
    )
    assert denied.is_error is True
    assert calls == []


def test_owner_and_valid_token_surfaces_are_unchanged() -> None:
    calls: list[str] = []
    registry = _registry(GUEST_ALLOWED | GUEST_DENIED, calls)

    owner = ToolContext(is_owner=True, caller_kind=CallerKind.AGENT)
    valid_token = ToolContext(is_owner=False, caller_kind=CallerKind.WEB)

    expected = GUEST_ALLOWED | GUEST_DENIED
    assert {tool.name for tool in registry.to_tool_definitions(owner)} == expected
    assert {tool.name for tool in registry.to_tool_definitions(valid_token)} == expected
