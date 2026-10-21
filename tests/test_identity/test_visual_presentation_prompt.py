"""WebUI presentation guidance stays scoped to its trusted entry surface."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.steps.prompt_cache import apply_prompt_cache
from opensquilla.engine.turn_runner.harness import _TurnRunnerSystemPromptRefreshAdapter
from opensquilla.identity.prompt import assemble_system_prompt
from opensquilla.identity.types import AgentProfile
from opensquilla.tools.types import CallerKind, ToolContext


@pytest.mark.parametrize("webui", [False, True])
@pytest.mark.parametrize("mode", ["full", "minimal", "none"])
@pytest.mark.parametrize("tools", [[], ["show_widget"], ["show_widget", "skill_view"]])
def test_visual_presentation_respects_surface_mode_and_visible_tools(webui, mode, tools):
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode=mode),
        tools=tools,
        webui_rendering=webui,
    )
    if not webui or mode != "full":
        assert "## Visual Presentation" not in prompt
        return
    section = prompt.split("## Visual Presentation\n", 1)[1].split("\n## ", 1)[0]
    assert "`mermaid`, `dot`, and `graphviz`" in section
    assert "when it sufficiently answers the request" in section
    assert ("use `show_widget`" in section) is ("show_widget" in tools)
    assert ("read the `visualize` Skill if it is listed" in section) is (
        "show_widget" in tools and "skill_view" in tools
    )


@pytest.mark.parametrize("cache_mode", ["off", "auto"])
async def test_runtime_keeps_visual_guidance_in_system_with_cache_on_or_off(
    tmp_path, monkeypatch, cache_mode,
):
    runner = TurnRunner(provider_selector=Mock())
    monkeypatch.setattr(runner, "_resolve_bootstrap_workspace_dir", lambda _agent: tmp_path)
    monkeypatch.setattr(runner, "_resolve_memory_source_dir", lambda _agent: tmp_path)
    prompt = runner._assemble_prompt(
        "main", [SimpleNamespace(name="show_widget"), SimpleNamespace(name="skill_view")],
        session_key="agent:main:synthetic",
        extra_context={"Turn fact": "request-only-marker"},
        webui_rendering=True,
    )
    assert isinstance(prompt, tuple)
    assert "## Visual Presentation" in prompt[0]
    assert "## Visual Presentation" not in prompt[1]
    turn = TurnContext(
        message="question", session_key="agent:main:synthetic",
        config=SimpleNamespace(prompt_cache=SimpleNamespace(effective_mode=cache_mode)),
        provider=None, model="synthetic", tool_defs=[], system_prompt=prompt,
    )
    await apply_prompt_cache(turn)
    system, breakpoints, request_context = runner._resolve_prompt_config(turn)
    assert "## Visual Presentation" in system
    assert "## Visual Presentation" not in (request_context or "")
    if cache_mode == "auto":
        assert "## Visual Presentation" in breakpoints[0]["text"]
        assert "request-only-marker" in request_context


@pytest.mark.parametrize("caller_kind", [CallerKind.WEB, CallerKind.CLI, CallerKind.CHANNEL])
def test_compaction_refresh_preserves_only_current_surface_guidance(caller_kind):
    from opensquilla.engine import Agent, AgentConfig

    assemble = Mock(side_effect=lambda *args, **kwargs: assemble_system_prompt(
        AgentProfile(agent_id="main"), tools=["show_widget", "skill_view"],
        webui_rendering=kwargs["webui_rendering"],
    ))
    agent = Agent(
        provider=Mock(), tool_context=ToolContext(caller_kind=caller_kind),
        config=AgentConfig(
            system_prompt="old", cache_breakpoints=[{"text": "old", "cache": "true"}],
        ),
    )
    _TurnRunnerSystemPromptRefreshAdapter(SimpleNamespace(_assemble_prompt=assemble)).refresh_system_prompt(
        agent=agent, agent_id="main", tool_defs=[], session_key="agent:main:webchat:synthetic",
        bootstrap_context_mode=None,
    )
    expected = caller_kind is CallerKind.WEB
    assert ("## Visual Presentation" in agent.config.system_prompt) is expected
    assert agent.config.cache_breakpoints[0]["text"] == agent.config.system_prompt
