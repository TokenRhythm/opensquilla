"""Idle compaction prepares the known request without running a user turn."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import CallerKind, ToolContext, ToolSpec


@pytest.mark.parametrize("cache_mode", ["off", "auto"])
def test_manual_envelope_has_real_prompt_and_tools_without_invocation(
    tmp_path, monkeypatch: pytest.MonkeyPatch, cache_mode: str,
) -> None:
    (tmp_path / "AGENTS.md").write_text("Use the project's approved conventions.", encoding="utf-8")
    handler = AsyncMock(side_effect=AssertionError("must not execute a tool"))
    registry = ToolRegistry()
    registry.register(
        ToolSpec("read_file", "Read a workspace file.", {"path": {"type": "string"}}),
        handler,
    )
    registry.register(
        ToolSpec("admin_private", "Owner-only capability.", {}, owner_only=True),
        handler,
    )
    registry.register(ToolSpec("write_file", "Write a workspace file.", {}), handler)
    provider = SimpleNamespace(
        provider_name="fixture-provider",
        model="fixture-model",
        chat=AsyncMock(side_effect=AssertionError("must not call the model")),
        stream=Mock(side_effect=AssertionError("must not stream from the model")),
    )
    config = SimpleNamespace(
        llm=SimpleNamespace(thinking="high"),
        prompt_cache=SimpleNamespace(effective_mode=cache_mode),
        compaction=SimpleNamespace(protected_recent_messages=5, compaction_profile="coding"),
        tools=SimpleNamespace(deny=["write_file"]),
    )
    runner = TurnRunner(provider_selector=Mock(), tool_registry=registry, config=config)
    monkeypatch.setattr(runner, "_resolve_bootstrap_workspace_dir", lambda _agent: tmp_path)
    monkeypatch.setattr(runner, "_resolve_memory_source_dir", lambda _agent: tmp_path)
    no_pipeline = AsyncMock(side_effect=AssertionError("must not execute a turn pipeline"))
    monkeypatch.setattr(runner, "_run_pipeline", no_pipeline)
    no_turn = AsyncMock(side_effect=AssertionError("must not execute a turn"))
    monkeypatch.setattr(runner, "_run_turn", no_turn)
    session = SimpleNamespace(
        session_key="agent:main:webchat:manual-envelope",
        agent_id="main",
        collaboration_mode="default",
    )

    agent = runner.prepare_manual_compaction_envelope(
        session,
        provider=provider,
        context_window_tokens=262_144,
        max_output_tokens=16_384,
        context_window_known=True,
        provider_request_max_chars=700_000,
        workspace_dir=str(tmp_path),
    )

    prompt = "\n".join(filter(None, [
        agent.config.system_prompt, agent.config.request_context_prompt,
    ]))
    assert "approved conventions." in prompt
    assert "read_file" in prompt
    assert [definition.name for definition in agent.tool_definitions] == ["read_file"]
    assert agent.config.model_id == "fixture-model"
    assert agent.config.provider_id == "fixture-provider"
    assert agent.config.context_window_tokens == 262_144
    assert agent.config.max_tokens == 16_384
    assert agent.config.provider_request_proof_max_chars == 700_000
    assert agent.config.compaction_profile == "coding"
    assert agent.config.compaction_protected_recent_messages == 5
    assert agent.config.thinking == "high"
    assert bool(agent.config.cache_breakpoints) is (cache_mode == "auto")
    assert agent.tool_handler is None
    assert agent._tool_context is None
    handler.assert_not_called()
    provider.chat.assert_not_called()
    provider.stream.assert_not_called()
    no_pipeline.assert_not_called()
    no_turn.assert_not_called()


def test_manual_envelope_copies_explicit_tool_policy_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    handler = AsyncMock(side_effect=AssertionError("must not execute a tool"))
    for name in ("read_file", "write_file"):
        registry.register(ToolSpec(name, name, {}), handler)
    runner = TurnRunner(provider_selector=Mock(), tool_registry=registry)
    monkeypatch.setattr(runner, "_assemble_prompt", lambda *_args, **_kwargs: "known instructions")
    caller = ToolContext(
        session_key="agent:main:webchat:manual-envelope",
        caller_kind=CallerKind.WEB,
        is_owner=True,
        denied_tools={"write_file"},
        surfaced_tools={"read_file"},
    )
    original_denied = set(caller.denied_tools)
    original_surfaced = set(caller.surfaced_tools)

    agent = runner.prepare_manual_compaction_envelope(
        SimpleNamespace(session_key=caller.session_key),
        provider=SimpleNamespace(provider_name="fixture", model="fixture-model"),
        context_window_tokens=256_000,
        max_output_tokens=8_000,
        context_window_known=True,
        provider_request_max_chars=600_000,
        workspace_dir=None,
        caller_tool_context=caller,
    )

    assert [definition.name for definition in agent.tool_definitions] == ["read_file"]
    assert caller.denied_tools == original_denied
    assert caller.surfaced_tools == original_surfaced
    assert caller.authorized_tool_names is None
    assert caller.disclosed_tool_names == set()
    assert agent.tool_handler is None
    handler.assert_not_called()
