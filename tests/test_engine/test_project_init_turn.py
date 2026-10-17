from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.agents.registry import AgentRegistry
from opensquilla.engine.commands import (
    DEFAULT_REGISTRY,
    CommandBusyPolicy,
    CommandCategory,
    CommandPresentation,
    Surface,
)
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.turn_runner.input_stage import InputStage, InputStageInput
from opensquilla.engine.types import ErrorEvent, RouterControlReplayEvent
from opensquilla.gateway.config import GatewayConfig, SquillaRouterConfig
from opensquilla.identity.project_instructions import load_project_rules
from opensquilla.provider import DoneEvent, TextDeltaEvent
from opensquilla.tools.builtin.agent_files import create_agent_file_tool
from opensquilla.tools.builtin.memory_tools import create_memory_tools
from opensquilla.tools.registry import ToolRegistry, get_default_registry
from opensquilla.tools.types import CallerKind, ToolContext


def _input(root: Path, *, provenance=None, message="/init", mode="user") -> InputStageInput:
    return InputStageInput(
        message=message, semantic_message=None, input_mode=mode,
        persist_input=False, input_provenance=provenance, session_key="agent:main:test",
        tool_context=ToolContext(workspace_dir=str(root)), session_append=None,
    )


def _stage() -> InputStage:
    return TurnRunner(provider_selector=None)._input_stage


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["/init", "//init", "> /init", "Explain /init"])
async def test_text_without_explicit_intent_never_expands(tmp_path: Path, message: str) -> None:
    output = await _stage().run(_input(tmp_path, message=message))
    assert output.project_init is None
    assert output.runtime_message == message


@pytest.mark.asyncio
async def test_explicit_init_freezes_template_without_changing_persisted_text(
    tmp_path: Path,
) -> None:
    calls = []

    class Append:
        async def append_message(self, key, role, content, *, provenance=None):
            calls.append(content)
            return SimpleNamespace(content="[timestamp] " + content)

    stage = _stage()
    inp = replace(
        _input(tmp_path, provenance={"turn_command": {"name": "project.init", "args": "notes"}}),
        persist_input=True, session_append=Append(),
    )
    output = await stage.run(inp)
    assert output.runtime_message == "[timestamp] /init"
    assert output.semantic_input == "/init"
    assert calls == ["/init"]
    assert output.project_init is not None
    (tmp_path / "agent.md").write_text("created between attempts")
    replay = await stage.run(replace(inp, persist_input=False, project_init=output.project_init))
    assert replay.project_init is output.project_init
    assert replay.project_init.selected_path == str(tmp_path / "AGENTS.md")


@pytest.mark.asyncio
async def test_init_missing_bound_workspace_fails_without_recreating(tmp_path: Path) -> None:
    missing = tmp_path / "gone"
    with pytest.raises(Exception, match="workspace|Workspace"):
        await _stage().run(_input(
            missing, provenance={"turn_command": {"name": "project.init", "args": ""}},
        ))
    assert not missing.exists()


def test_init_catalog_exposes_only_completed_web_adapter() -> None:
    command = DEFAULT_REGISTRY.find("/init", Surface.WEB_CHAT)
    assert command is not None
    assert command.category is CommandCategory.TURN
    assert command.busy_policy is CommandBusyPolicy.NEXT_TURN
    assert command.presentation is CommandPresentation.TURN
    assert command.execution_for(Surface.WEB_CHAT).action == "project.init"
    assert DEFAULT_REGISTRY.find("/init", Surface.CLI_STANDALONE) is None
    assert DEFAULT_REGISTRY.find("/init", Surface.CHANNEL) is None


def test_project_rule_dynamic_snapshot_and_bootstrap_dedup(tmp_path: Path) -> None:
    source = tmp_path / "AGENTS.md"
    source.write_text("rule version alpha")
    runner = TurnRunner(provider_selector=None, config=SimpleNamespace(workspace_dir=str(tmp_path)))
    frozen = load_project_rules(str(tmp_path))
    first = runner._assemble_prompt(
        "main", [], session_key="agent:main:test", workspace_dir=str(tmp_path),
        project_rules_snapshot=frozen,
    )
    source.write_text("rule version beta")
    replay = runner._assemble_prompt(
        "main", [], session_key="agent:main:test", workspace_dir=str(tmp_path),
        project_rules_snapshot=frozen,
    )
    next_turn = runner._assemble_prompt(
        "main", [], session_key="agent:main:test", workspace_dir=str(tmp_path),
    )
    assert first == replay
    assert isinstance(first, tuple) and isinstance(next_turn, tuple)
    assert "rule version alpha" not in first[0]
    assert first[1].count("rule version alpha") == 1
    assert "rule version alpha" not in "\n".join(next_turn)
    assert next_turn[1].count("rule version beta") == 1
    side_turn = runner._assemble_prompt(
        "main", [], session_key="agent:main:side", workspace_dir=str(tmp_path),
    )
    assert "rule version beta" in side_turn[1]
    assert "rule version alpha" in frozen.render()


@pytest.mark.parametrize("use_legacy", [False, True])
def test_deleted_project_rules_do_not_reappear_from_bootstrap_cache(
    tmp_path: Path, use_legacy: bool,
) -> None:
    source = tmp_path / "AGENTS.md"
    source.write_text("deleted rule must disappear")
    runner = TurnRunner(provider_selector=None, config=SimpleNamespace(workspace_dir=str(tmp_path)))
    runner._assemble_prompt(
        "main", [], session_key="agent:main:test", workspace_dir=str(tmp_path),
    )
    source.unlink()
    if use_legacy:
        (tmp_path / "agent.md").write_text("remaining legacy rule")
    after = runner._assemble_prompt(
        "main", [], session_key="agent:main:test", workspace_dir=str(tmp_path),
    )
    assert "deleted rule must disappear" not in str(after)
    assert ("remaining legacy rule" in str(after)) is use_legacy


class _Provider:
    provider_name = "test"
    model = "test/model"

    def __init__(self):
        self.calls = []

    async def chat(self, messages, tools=None, config=None):
        self.calls.append((messages, tools, config))
        yield TextDeltaEvent(text="Analysis complete.")
        yield DoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)


class _Selector:
    def __init__(self, provider):
        self.provider = provider
        self.current_config = SimpleNamespace(model="test/model")

    def clone(self):
        return self

    def resolve(self):
        return self.provider

    def override_model(self, model):
        self.current_config = SimpleNamespace(model=model)
        self.provider.model = model


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", [False, True])
async def test_real_turn_surfaces_source_tools_without_bypassing_explicit_deny(
    tmp_path: Path, denied: bool,
) -> None:
    (tmp_path / "AGENTS.md").write_text("local project rule")
    provider = _Provider()
    runner = TurnRunner(
        provider_selector=_Selector(provider), tool_registry=get_default_registry(),
        config=GatewayConfig(
            workspace_dir=str(tmp_path), squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    context = ToolContext(
        is_owner=True, caller_kind=CallerKind.WEB, workspace_dir=str(tmp_path),
        denied_tools={"edit_source"} if denied else set(),
    )
    events = [event async for event in runner.run(
        "/init", "agent:main:test", tool_context=context,
        input_provenance={"turn_command": {"name": "project.init", "args": "Use real commands"}},
        history_has_persisted_user=False, no_memory_capture=True,
    )]
    assert not [event for event in events if isinstance(event, ErrorEvent)]
    assert provider.calls
    messages, tools, config = provider.calls[0]
    message_text = str(messages)
    assert "The user invoked /init" in message_text
    assert "local project rule" in message_text + str(config)
    if denied:
        assert "required source tools are unavailable" in message_text
        assert "Use read_source before editing" not in message_text
    else:
        assert "Use read_source before editing" in message_text
    assert context.surfaced_tools is None


@pytest.mark.asyncio
async def test_router_replay_reuses_rule_and_init_snapshots(tmp_path: Path, monkeypatch) -> None:
    from opensquilla.engine import runtime

    (tmp_path / "agent.md").write_text("rule before replay")
    provider = _Provider()
    runner = TurnRunner(
        provider_selector=_Selector(provider), tool_registry=get_default_registry(),
        config=GatewayConfig(
            workspace_dir=str(tmp_path), squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    original_loader = runtime.load_project_rules
    loads = []

    def record_load(*args, **kwargs):
        loads.append(args[0])
        return original_loader(*args, **kwargs)

    monkeypatch.setattr(runtime, "load_project_rules", record_load)
    original_stream = runner._stream_consumer_stage.run
    stream_count = 0

    async def replay_once(inp):
        nonlocal stream_count
        stream_count += 1
        if stream_count == 1:
            (tmp_path / "AGENTS.md").write_text("rule appeared during replay")
            yield RouterControlReplayEvent(action="test", replay_depth=1)
            return
        async for event in original_stream(inp):
            yield event

    monkeypatch.setattr(runner._stream_consumer_stage, "run", replay_once)
    context = ToolContext(is_owner=True, caller_kind=CallerKind.WEB, workspace_dir=str(tmp_path))
    events = [event async for event in runner.run(
        "/init", "agent:main:replay", tool_context=context,
        input_provenance={"turn_command": {"name": "project.init", "args": ""}},
        history_has_persisted_user=False, no_memory_capture=True,
    )]
    assert not [event for event in events if isinstance(event, ErrorEvent)]
    assert len(loads) == 1
    projected = str(provider.calls[0])
    assert "rule before replay" in projected
    assert "rule appeared during replay" not in projected
    assert str(tmp_path / "agent.md").replace("\\", "\\\\") in projected


def test_compaction_refresh_never_reloads_project_rules(tmp_path: Path, monkeypatch) -> None:
    from opensquilla.engine import runtime
    from opensquilla.engine.turn_runner.harness import _TurnRunnerSystemPromptRefreshAdapter

    (tmp_path / "AGENTS.md").write_text("initial rule")
    runner = TurnRunner(provider_selector=None, config=SimpleNamespace(workspace_dir=str(tmp_path)))
    original = runner._assemble_prompt(
        "main", [], session_key="agent:main:compact", workspace_dir=str(tmp_path),
    )
    assert isinstance(original, tuple)
    (tmp_path / "AGENTS.md").write_text("changed during turn")

    def unexpected_load(*args, **kwargs):
        raise AssertionError("compaction must retain the existing request context")

    monkeypatch.setattr(runtime, "load_project_rules", unexpected_load)
    refreshed = []
    agent = SimpleNamespace(
        config=SimpleNamespace(workspace_dir=str(tmp_path)), _tool_context=None,
        refresh_system_prompt=refreshed.append,
    )
    _TurnRunnerSystemPromptRefreshAdapter(runner).refresh_system_prompt(
        agent=agent, agent_id="main", tool_defs=[], session_key="agent:main:compact",
        bootstrap_context_mode=None,
    )
    assert refreshed == [original[0]]
    assert "initial rule" in original[1]


@pytest.mark.parametrize("mode", ["stateless", "heartbeat_light"])
def test_existing_light_context_modes_do_not_gain_project_rules(tmp_path: Path, mode: str) -> None:
    (tmp_path / "AGENTS.md").write_text("project rule")
    runner = TurnRunner(provider_selector=None, config=SimpleNamespace(workspace_dir=str(tmp_path)))
    assembled = runner._assemble_prompt(
        "main", [], workspace_dir=str(tmp_path), bootstrap_context_mode=mode,
    )
    assert "project rule" not in str(assembled)


@pytest.mark.asyncio
@pytest.mark.parametrize("deny_save", [False, True])
async def test_discoverable_persistence_tools_guide_prompt_without_expanding_schema(
    tmp_path: Path, deny_save: bool,
) -> None:
    provider = _Provider()
    config = GatewayConfig(
        workspace_dir=str(tmp_path), squilla_router=SquillaRouterConfig(enabled=False),
    )
    registry = ToolRegistry()
    search = get_default_registry().get("tool_search")
    assert search is not None
    registry.register(search.spec, search.handler)
    create_memory_tools(object(), object(), registry=registry, memory_dir=str(tmp_path))
    create_agent_file_tool(AgentRegistry(config, persist_changes=False), registry=registry)
    runner = TurnRunner(
        provider_selector=_Selector(provider), tool_registry=registry, config=config,
    )
    context = ToolContext(
        is_owner=True, caller_kind=CallerKind.WEB, workspace_dir=str(tmp_path),
        allowed_tools={"tool_search", "agent_file", "memory_get", "memory_save"},
        denied_tools={"memory_save"} if deny_save else set(),
    )
    events = [event async for event in runner.run(
        "Remember my preferred report format", "agent:main:memory", tool_context=context,
        history_has_persisted_user=False, no_memory_capture=True,
    )]
    assert not [event for event in events if isinstance(event, ErrorEvent)]
    messages, definitions, chat_config = provider.calls[0]
    assert "memory_save" not in {tool.name for tool in definitions}
    assert "agent_file" in {tool.name for tool in definitions}
    prompt = str(chat_config)
    assert "Use agent_file for USER.md" in prompt
    assert ("Use memory_save for durable non-profile facts" in prompt) is not deny_save
