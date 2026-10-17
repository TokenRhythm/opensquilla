from __future__ import annotations

import asyncio
import json

import pytest

from opensquilla.agents import files
from opensquilla.agents.registry import AgentRegistry
from opensquilla.gateway.config import GatewayConfig
from opensquilla.tools.builtin.agent_files import create_agent_file_tool
from opensquilla.tools.policy_runtime import resolve_runtime_tool_surface
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import CallerKind, ToolContext, ToolError, current_tool_context


def _tool(tmp_path):
    config = GatewayConfig(workspace_dir=str(tmp_path / "agent-source"))
    registry = ToolRegistry()
    agents = AgentRegistry(config, persist_changes=False)
    create_agent_file_tool(agents, registry=registry)
    return registry, registry.get("agent_file").handler, agents


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [CallerKind.AGENT, CallerKind.WEB, CallerKind.CLI])
async def test_profile_read_is_pure_and_replace_uses_agent_source(tmp_path, caller):
    registry, tool, _agents = _tool(tmp_path)
    task = tmp_path / "task"
    task.mkdir()
    (task / "USER.md").write_text("Project deliverable", encoding="utf-8")
    writes = []
    original_ctx = ToolContext(
        is_owner=True, workspace_dir=str(task), agent_id="ops",
        caller_kind=caller,
        on_bootstrap_source_write=lambda agent, name: writes.append((agent, name)),
    )
    ctx = resolve_runtime_tool_surface(original_ctx)
    assert original_ctx.surfaced_tools is None
    token = current_tool_context.set(ctx)
    try:
        read = json.loads(await tool(action="read", name="USER.md"))
        assert read == {"name": "USER.md", "content": "", "exists": False, "revision": "absent"}
        assert not (tmp_path / "agent-source").exists()
        with pytest.raises(ToolError, match="expected_revision"):
            await tool(action="replace", name="USER.md", content="not authorized by read")
        result = json.loads(await tool(
            action="replace", name="USER.md", content="Name: Mira\n",
            expected_revision=read["revision"],
        ))
        assert result["revision"].startswith("sha256:")
        assert "agent_file" in {item.name for item in registry.to_tool_definitions(ctx)}
        with pytest.raises(ToolError, match="Only USER.md"):
            await tool(action="read", name="AGENTS.md")
    finally:
        current_tool_context.reset(token)
    source = tmp_path / "agent-source" / "agents" / "ops"
    assert {item.name for item in source.iterdir()} == {"USER.md"}
    assert (source / "USER.md").read_bytes() == b"Name: Mira\n"
    assert (task / "USER.md").read_text(encoding="utf-8") == "Project deliverable"
    assert writes == [("ops", "USER.md")]


@pytest.mark.asyncio
async def test_two_replacements_of_one_revision_have_one_winner(tmp_path):
    _registry, tool, agents = _tool(tmp_path)
    await agents.set_agent_file("main", "SOUL.md", "original")
    token = current_tool_context.set(resolve_runtime_tool_surface(ToolContext(is_owner=True)))
    try:
        original = json.loads(await tool(action="read", name="SOUL.md"))
        results = await asyncio.gather(*(
            tool(action="replace", name="SOUL.md", content=content,
                 expected_revision=original["revision"])
            for content in ("first", "second")
        ), return_exceptions=True)
        assert sum(isinstance(item, str) for item in results) == 1
        assert sum(isinstance(item, files.AgentFileConflictError) for item in results) == 1
        current = json.loads(await tool(action="read", name="SOUL.md"))
        assert current["content"] in {"first", "second"}
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [
    None,
    ToolContext(is_owner=False),
    ToolContext(is_owner=True, guest_safe=True),
    ToolContext(is_owner=True, caller_kind=CallerKind.SUBAGENT),
    ToolContext(is_owner=True, caller_kind=CallerKind.CRON),
    ToolContext(is_owner=True, caller_kind=CallerKind.CHANNEL),
    ToolContext(is_owner=True, caller_kind=CallerKind.CHANNEL,
                channel_admin_verified=True, channel_kind="group"),
])
async def test_profile_tool_rejects_unproven_or_background_callers(tmp_path, context):
    registry, tool, _agents = _tool(tmp_path)
    token = current_tool_context.set(context)
    try:
        if context is not None:
            assert "agent_file" not in {item.name for item in registry.to_tool_definitions(context)}
        with pytest.raises(ToolError, match="authorized owner"):
            await tool(action="read", name="USER.md")
    finally:
        current_tool_context.reset(token)
    assert not (tmp_path / "agent-source").exists()


@pytest.mark.asyncio
async def test_profile_tool_allows_existing_verified_private_channel_owner(tmp_path):
    _registry, tool, _agents = _tool(tmp_path)
    ctx = ToolContext(
        is_owner=True, caller_kind=CallerKind.CHANNEL, channel_admin_verified=True,
        channel_kind="dm", session_key="agent:main:telegram:dm:owner",
    )
    token = current_tool_context.set(resolve_runtime_tool_surface(ctx))
    try:
        assert json.loads(await tool(action="read", name="USER.md"))["revision"] == "absent"
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("restrictions", [
    {"denied_tools": {"agent_file"}},
    {"allowed_tools": {"read_file"}},
])
async def test_owner_surfacing_does_not_override_tool_policy(tmp_path, restrictions):
    registry, tool, _agents = _tool(tmp_path)
    ctx = resolve_runtime_tool_surface(ToolContext(
        is_owner=True, caller_kind=CallerKind.WEB, **restrictions,
    ))
    assert "agent_file" not in {item.name for item in registry.to_tool_definitions(ctx)}
    token = current_tool_context.set(ctx)
    try:
        with pytest.raises(ToolError, match="tool permissions"):
            await tool(action="read", name="USER.md")
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_external_edit_conflicts_without_writing_or_invalidating(tmp_path):
    _registry, _tool_handler, agents = _tool(tmp_path)
    await agents.set_agent_file("main", "IDENTITY.md", "first")
    snapshot = await agents.get_agent_file("main", "IDENTITY.md")
    source = tmp_path / "agent-source" / "IDENTITY.md"
    source.write_text("editor change", encoding="utf-8")
    callbacks = []
    with pytest.raises(files.AgentFileConflictError):
        await agents.set_agent_file(
            "main", "IDENTITY.md", "stale", expected_revision=snapshot["revision"],
            on_write=lambda: callbacks.append("changed"),
        )
    assert source.read_text(encoding="utf-8") == "editor change"
    assert callbacks == []


@pytest.mark.asyncio
async def test_write_readback_mismatch_is_not_reported_as_success(tmp_path, monkeypatch):
    _registry, _tool_handler, agents = _tool(tmp_path)
    source = tmp_path / "agent-source" / "USER.md"
    original_write = files.os.write
    callbacks = []

    def concurrent_edit(fd, data):
        count = original_write(fd, data)
        source.write_text("external editor", encoding="utf-8")
        return count

    monkeypatch.setattr(files.os, "write", concurrent_edit)
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        await agents.set_agent_file(
            "main", "USER.md", "intended", expected_revision="absent",
            on_write=lambda: callbacks.append("invalidated"),
        )
    assert source.read_text(encoding="utf-8") == "external editor"
    assert callbacks == ["invalidated"]
