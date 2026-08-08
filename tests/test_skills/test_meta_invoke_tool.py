"""Tests for meta_invoke tool registration and Agent dispatch interception.

This file accumulates tests across Tasks 1, 3, 5, 6 of the
meta_invoke-soft-activation plan. Task 1 covers registration only.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_meta_invoke_registered_in_default_registry() -> None:
    """meta_invoke appears in the registry after importing the builtin
    module."""
    # Importing the builtin package triggers all registrations.
    from opensquilla.tools.builtin import meta_tools  # noqa: F401 — import side-effect
    from opensquilla.tools.registry import get_default_registry

    assert get_default_registry().get("meta_invoke") is not None


def test_meta_invoke_spec_shape() -> None:
    """meta_invoke advertises a single required string parameter 'name',
    and the description mentions meta-skill semantics."""
    from opensquilla.tools.builtin import meta_tools  # noqa: F401
    from opensquilla.tools.registry import get_default_registry

    registered = get_default_registry().get("meta_invoke")
    assert registered is not None
    spec = registered.spec
    assert spec.name == "meta_invoke"
    assert "name" in spec.parameters
    assert spec.required == ["name"]
    # Description must mention meta-skill semantics for the LLM
    desc = spec.description.lower()
    assert "meta-skill" in desc
    assert "playbook" in desc or "multi-step" in desc


def test_meta_invoke_not_exposed_by_default() -> None:
    """meta_invoke must not appear in default tool catalogues. It is
    conditionally surfaced by SkillInjector when meta-skills are present."""
    from opensquilla.tools.builtin import meta_tools  # noqa: F401
    from opensquilla.tools.registry import get_default_registry

    registered = get_default_registry().get("meta_invoke")
    assert registered is not None  # exists in registry
    assert registered.spec.exposed_by_default is False, (
        "meta_invoke should be conditionally surfaced, not always exposed"
    )


@pytest.mark.asyncio
async def test_meta_invoke_handler_raises_routing_error() -> None:
    """If the standard dispatcher ever invokes the meta_invoke handler,
    that's a configuration bug — the Agent's dispatch loop should have
    intercepted it. Raise a clear RuntimeError naming the expected
    interception point."""
    from opensquilla.tools.builtin.meta_tools import meta_invoke

    with pytest.raises(RuntimeError) as exc_info:
        await meta_invoke(name="any")
    msg = str(exc_info.value).lower()
    assert "agent" in msg or "_run_one_streaming" in msg or "intercept" in msg


# ---------------------------------------------------------------------------
# Task 3: ToolResult.terminates_turn field + preservation through
# Agent._compress_tool_result rebuild sites.
# ---------------------------------------------------------------------------


def test_tool_result_has_terminates_turn_field() -> None:
    """ToolResult.terminates_turn defaults to False; can be set True."""
    from opensquilla.tool_boundary import ToolResult

    r = ToolResult(tool_use_id="u1", tool_name="t", content="ok")
    assert r.terminates_turn is False

    r2 = ToolResult(
        tool_use_id="u1", tool_name="t", content="ok", terminates_turn=True,
    )
    assert r2.terminates_turn is True


class _NullProvider:
    """Minimal LLMProvider stand-in: never called by _compress_tool_result."""

    provider_name = "null"

    def chat(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
        raise AssertionError("provider.chat must not be called by _compress_tool_result")

    async def list_models(self) -> list[object]:  # pragma: no cover
        return []


@pytest.mark.asyncio
async def test_compress_tool_result_preserves_terminates_turn_when_short() -> None:
    """When content is short enough to not need compression, the rebuild
    must still carry terminates_turn through."""
    from opensquilla.engine import Agent, AgentConfig
    from opensquilla.tool_boundary import ToolResult

    agent = Agent(provider=_NullProvider(), config=AgentConfig())

    original = ToolResult(
        tool_use_id="u1",
        tool_name="meta_invoke",
        content="small content",
        is_error=False,
        terminates_turn=True,
    )
    compressed = await agent._compress_tool_result(original)
    assert compressed.terminates_turn is True


@pytest.mark.asyncio
async def test_compress_tool_result_preserves_terminates_turn_when_compressed() -> None:
    """When content IS large enough to trigger compression, the rebuild
    must STILL carry terminates_turn through (the other code path)."""
    from opensquilla.engine import Agent, AgentConfig
    from opensquilla.tool_boundary import ToolResult

    # Shrink context_window_tokens so 50_000 chars (~12500 tokens) exceeds
    # the compression budget (context_window_tokens * max_share = 1000 * 0.25
    # = 250 tokens). truncate mode keeps compression purely local — no
    # provider call needed.
    config = AgentConfig(
        context_window_tokens=1000,
        tool_result_compression_enabled=True,
        tool_result_compression_mode="truncate",
    )
    agent = Agent(provider=_NullProvider(), config=config)

    big_content = "x" * 50_000
    original = ToolResult(
        tool_use_id="u1",
        tool_name="meta_invoke",
        content=big_content,
        is_error=False,
        terminates_turn=True,
    )
    compressed = await agent._compress_tool_result(original)
    # Sanity-check the compression path actually fired (content shrunk).
    assert len(compressed.content) < len(big_content), (
        "test setup error: compression did not trigger; "
        "second rebuild site would not be exercised"
    )
    # The FLAG must survive the rebuild.
    assert compressed.terminates_turn is True, (
        "terminates_turn lost during ToolResult compression rebuild"
    )


# ---------------------------------------------------------------------------
# Task 5: Agent._run_one_streaming for meta_invoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_one_streaming_success_yields_events_then_terminating_result(
    tmp_path,
) -> None:
    """Agent._run_one_streaming for a successful meta-skill yields nested
    events then a ToolResult with terminates_turn=True and is_error=False."""
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.tool_boundary import ToolCall, ToolResult
    from opensquilla.tools.builtin import meta_tools  # noqa: F401 — registers meta_invoke
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext

    # Synthesize a tiny meta-skill using kind: meta directly (bypassing
    # the SOP markdown compiler so llm_classify is supported).
    bundled = tmp_path / "skills" / "bundled"
    bundled.mkdir(parents=True)
    skill_dir = bundled / "meta-tiny"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: meta-tiny\n"
        "kind: meta\n"
        "description: tiny meta-skill\n"
        "triggers: [tiny-meta-trigger]\n"
        "composition:\n"
        "  steps:\n"
        "    - id: c\n"
        "      kind: llm_classify\n"
        "      output_choices: [A, B]\n"
        "      with: {text: \"x\"}\n"
        "---\n"
        "# meta-tiny\n",
        encoding="utf-8",
    )
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    loader.load_all()

    spec = loader.get_by_name("meta-tiny")
    assert spec is not None
    assert getattr(spec, "kind", None) == "meta"

    class _NullProvider:
        provider_name = "null"

        async def chat(self, *_args, **_kwargs):
            raise AssertionError("provider.chat must not be called in this test")

        async def list_models(self):
            return []

    registry = get_default_registry()
    assert registry.get("meta_invoke") is not None

    config = AgentConfig(
        model_id="stub",
        max_iterations=1,
        system_prompt="",
        metadata={"skill_loader": loader, "bootstrap_workspace_dir": str(tmp_path)},
    )

    agent = Agent(
        provider=_NullProvider(),  # type: ignore[arg-type]
        config=config,
        tool_definitions=[],
        tool_handler=None,
        tool_registry=registry,
    )

    async def fake_llm_chat(_s: str, _u: str) -> str:
        return "A"

    agent._test_llm_chat_override = fake_llm_chat  # type: ignore[attr-defined]

    tc = ToolCall(
        tool_use_id="u1",
        tool_name="meta_invoke",
        arguments={"name": "meta-tiny"},
    )
    tool_ctx = ToolContext(workspace_dir=str(tmp_path), is_owner=True)

    events = []
    final = None
    async for ev in agent._run_one_streaming(tc, tool_ctx):
        if isinstance(ev, ToolResult):
            final = ev
        else:
            events.append(ev)

    assert final is not None, "should yield a final ToolResult"
    assert final.is_error is False, f"expected success but got: {final.content!r}"
    assert final.terminates_turn is True
    # Permissive content check — the deliverable should mention or carry the
    # classifier output, but exact wording depends on orchestrator framing.
    assert final.content


@pytest.mark.asyncio
async def test_meta_invoke_rejects_global_provider_alias_for_untrusted_parent(
    tmp_path,
    monkeypatch,
) -> None:
    """A non-authorized parent cannot turn a global alias into child auth."""
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig
    from opensquilla.skills.types import SkillPlatformMeta, SkillRequires
    from opensquilla.tool_boundary import ToolCall, ToolResult
    from opensquilla.tools.builtin import meta_tools  # noqa: F401
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext
    from tests.test_engine.test_runtime_meta_invoke_surfacing import (
        _make_loader_with_meta,
    )

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    secret = "synthetic-config-only-openrouter-key"
    loader = _make_loader_with_meta(tmp_path)
    spec = loader.get_by_name("meta-tiny")
    assert spec is not None
    spec.metadata = SkillPlatformMeta(
        requires=SkillRequires(env_any=["OPENROUTER_API_KEY"])
    )

    class _NullProvider:
        provider_name = "null"

        async def chat(self, *_args, **_kwargs):
            raise AssertionError("provider.chat must not be called in this test")

        async def list_models(self):
            return []

    agent = Agent(
        provider=_NullProvider(),  # type: ignore[arg-type]
        config=AgentConfig(
            model_id="stub",
            max_iterations=1,
            metadata={
                "skill_loader": loader,
                "bootstrap_workspace_dir": str(tmp_path),
                "meta_readiness_env_aliases": lambda _parent, _plan: (
                    "OPENROUTER_API_KEY",
                ),
                "meta_skill_runtime_env_provider": lambda _parent, _plan: {
                    "nano-banana-pro": {"OPENROUTER_API_KEY": secret},
                },
            },
        ),
        tool_definitions=[],
        tool_handler=None,
        tool_registry=get_default_registry(),
    )

    async def fake_llm_chat(_system: str, _user: str) -> str:
        return "A"

    agent._test_llm_chat_override = fake_llm_chat  # type: ignore[attr-defined]
    captured: dict[str, object] = {}
    original_builder = agent._build_meta_orchestrator

    def capture_builder(**kwargs: object):
        result = original_builder(**kwargs)
        captured["skill_runtime_env"] = result[0]._skill_runtime_env
        captured["triggered_by"] = kwargs.get("triggered_by")
        return result

    monkeypatch.setattr(agent, "_build_meta_orchestrator", capture_builder)
    tool_call = ToolCall(
        tool_use_id="u-config-key",
        tool_name="meta_invoke",
        arguments={"name": "meta-tiny"},
    )
    final = None
    events = []
    async for event in agent._run_one_streaming(
        tool_call,
        ToolContext(workspace_dir=str(tmp_path), is_owner=True),
    ):
        events.append(event)
        if isinstance(event, ToolResult):
            final = event

    assert final is not None
    assert final.is_error is True
    assert "requires setup" in final.content
    assert secret not in final.content
    assert captured == {}
    assert secret not in repr(tool_call.arguments)
    assert secret not in repr(events)
    assert secret not in repr(agent.config.metadata)


@pytest.mark.asyncio
async def test_meta_invoke_llm_chat_step_records_usage(tmp_path) -> None:
    """Meta-skill llm_chat steps call the provider outside the normal Agent
    loop, but their tokens still belong to the parent session usage."""
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig
    from opensquilla.engine.usage import UsageTracker
    from opensquilla.provider.types import DoneEvent as ProviderDoneEvent
    from opensquilla.provider.types import TextDeltaEvent as ProviderTextDeltaEvent
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.tool_boundary import ToolCall, ToolResult
    from opensquilla.tools.builtin import meta_tools  # noqa: F401
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext

    bundled = tmp_path / "skills" / "bundled"
    skill_dir = bundled / "meta-usage"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: meta-usage\n"
        "kind: meta\n"
        "description: usage accounting meta-skill\n"
        "final_text_mode: raw\n"
        "triggers: [usage accounting]\n"
        "composition:\n"
        "  steps:\n"
        "    - id: write\n"
        "      kind: llm_chat\n"
        "      with:\n"
        "        system: s\n"
        "        task: t\n"
        "---\n"
        "# meta-usage\n",
        encoding="utf-8",
    )
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    loader.load_all()

    class _UsageProvider:
        provider_name = "stub"

        async def chat(self, *_args, **_kwargs):
            yield ProviderTextDeltaEvent(text="done")
            yield ProviderDoneEvent(
                input_tokens=11,
                output_tokens=7,
                cached_tokens=3,
                cache_write_tokens=2,
                model="stub/meta",
            )

        async def list_models(self):
            return []

    usage = UsageTracker()
    agent = Agent(
        provider=_UsageProvider(),  # type: ignore[arg-type]
        config=AgentConfig(
            model_id="stub/base",
            metadata={"skill_loader": loader, "bootstrap_workspace_dir": str(tmp_path)},
        ),
        tool_definitions=[],
        tool_handler=None,
        tool_registry=get_default_registry(),
        usage_tracker=usage,
        session_key="agent:main:test-usage",
    )

    final = None
    async for ev in agent._run_one_streaming(
        ToolCall(
            tool_use_id="u1",
            tool_name="meta_invoke",
            arguments={"name": "meta-usage"},
        ),
        ToolContext(workspace_dir=str(tmp_path), is_owner=True),
    ):
        if isinstance(ev, ToolResult):
            final = ev

    assert final is not None
    assert final.is_error is False
    tracked = usage.get("agent:main:test-usage")
    assert tracked is not None
    assert tracked.input_tokens == 11
    assert tracked.output_tokens == 7
    assert tracked.cache_read_tokens == 3
    assert tracked.cache_write_tokens == 2
    assert tracked.model_id == "stub/meta"


@pytest.mark.asyncio
async def test_run_one_streaming_unknown_meta_skill_returns_error_result(
    tmp_path,
) -> None:
    """meta_invoke with an unknown name yields ToolResult(is_error=True,
    terminates_turn=False)."""
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.tool_boundary import ToolCall, ToolResult
    from opensquilla.tools.builtin import meta_tools  # noqa: F401 — registers meta_invoke
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext

    bundled = tmp_path / "skills" / "bundled"
    bundled.mkdir(parents=True)
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    loader.load_all()

    class _NullProvider:
        provider_name = "null"

        async def chat(self, *_args, **_kwargs):
            raise AssertionError("provider.chat must not be called in this test")

        async def list_models(self):
            return []

    registry = get_default_registry()
    assert registry.get("meta_invoke") is not None

    config = AgentConfig(
        model_id="stub",
        max_iterations=1,
        system_prompt="",
        metadata={"skill_loader": loader, "bootstrap_workspace_dir": str(tmp_path)},
    )
    agent = Agent(
        provider=_NullProvider(),  # type: ignore[arg-type]
        config=config,
        tool_definitions=[],
        tool_handler=None,
        tool_registry=registry,
    )

    tc = ToolCall(
        tool_use_id="u1",
        tool_name="meta_invoke",
        arguments={"name": "nonexistent-meta-skill"},
    )
    tool_ctx = ToolContext(workspace_dir=str(tmp_path), is_owner=True)

    final = None
    async for ev in agent._run_one_streaming(tc, tool_ctx):
        if isinstance(ev, ToolResult):
            final = ev

    assert final is not None
    assert final.is_error is True
    assert final.terminates_turn is False
    assert "not a registered meta-skill" in final.content


@pytest.mark.asyncio
async def test_run_one_streaming_rejects_disabled_meta_skill(tmp_path) -> None:
    """meta_invoke must not bypass disable-model-invocation."""
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.tool_boundary import ToolCall, ToolResult
    from opensquilla.tools.builtin import meta_tools  # noqa: F401
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext

    bundled = tmp_path / "skills" / "bundled"
    skill_dir = bundled / "meta-hidden"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: meta-hidden\n"
        "kind: meta\n"
        "description: hidden meta-skill\n"
        "disable-model-invocation: true\n"
        "triggers: [hidden trigger]\n"
        "composition:\n"
        "  steps:\n"
        "    - id: c\n"
        "      kind: llm_classify\n"
        "      output_choices: [A, B]\n"
        "      with: {text: \"x\"}\n"
        "---\n"
        "# meta-hidden\n",
        encoding="utf-8",
    )
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    loader.load_all()

    class _NullProvider:
        provider_name = "null"

        async def chat(self, *_args, **_kwargs):
            raise AssertionError("disabled meta-skill must not execute")

        async def list_models(self):
            return []

    agent = Agent(
        provider=_NullProvider(),  # type: ignore[arg-type]
        config=AgentConfig(
            model_id="stub",
            metadata={"skill_loader": loader, "bootstrap_workspace_dir": str(tmp_path)},
        ),
        tool_definitions=[],
        tool_handler=None,
        tool_registry=get_default_registry(),
    )
    tc = ToolCall(
        tool_use_id="u1",
        tool_name="meta_invoke",
        arguments={"name": "meta-hidden"},
    )
    tool_ctx = ToolContext(
        workspace_dir=str(tmp_path),
        is_owner=True,
        allowed_tools={"meta_invoke"},
        surfaced_tools={"meta_invoke"},
    )

    final = None
    async for ev in agent._run_one_streaming(tc, tool_ctx):
        if isinstance(ev, ToolResult):
            final = ev

    assert final is not None
    assert final.is_error is True
    assert "not available for model invocation" in final.content
    assert final.terminates_turn is False


@pytest.mark.asyncio
async def test_run_one_streaming_rejects_meta_invoke_when_meta_skill_config_disabled(
    tmp_path,
) -> None:
    """meta_invoke must not bypass the global meta-skill switch."""
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.tool_boundary import ToolCall, ToolResult
    from opensquilla.tools.builtin import meta_tools  # noqa: F401
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext

    bundled = tmp_path / "skills" / "bundled"
    skill_dir = bundled / "meta-visible"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: meta-visible\n"
        "kind: meta\n"
        "description: visible meta-skill\n"
        "triggers: [visible trigger]\n"
        "composition:\n"
        "  steps:\n"
        "    - id: c\n"
        "      kind: llm_classify\n"
        "      output_choices: [A, B]\n"
        "      with: {text: \"x\"}\n"
        "---\n"
        "# meta-visible\n",
        encoding="utf-8",
    )
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    loader.load_all()

    class _NullProvider:
        provider_name = "null"

        async def chat(self, *_args, **_kwargs):
            raise AssertionError("globally disabled meta-skill must not execute")

        async def list_models(self):
            return []

    agent = Agent(
        provider=_NullProvider(),  # type: ignore[arg-type]
        config=AgentConfig(
            model_id="stub",
            metadata={
                "skill_loader": loader,
                "bootstrap_workspace_dir": str(tmp_path),
                "meta_skill_enabled": False,
            },
        ),
        tool_definitions=[],
        tool_handler=None,
        tool_registry=get_default_registry(),
    )
    tc = ToolCall(
        tool_use_id="u1",
        tool_name="meta_invoke",
        arguments={"name": "meta-visible"},
    )
    tool_ctx = ToolContext(
        workspace_dir=str(tmp_path),
        is_owner=True,
        allowed_tools={"meta_invoke"},
        surfaced_tools={"meta_invoke"},
    )

    final = None
    async for ev in agent._run_one_streaming(tc, tool_ctx):
        if isinstance(ev, ToolResult):
            final = ev

    assert final is not None
    assert final.is_error is True
    assert "meta-skill is disabled" in final.content
    assert final.terminates_turn is False


@pytest.mark.asyncio
async def test_run_one_streaming_propagates_current_turn_message_to_inputs(
    tmp_path,
) -> None:
    """The user's run_turn(message=...) text must flow into MetaMatch.inputs
    as user_message — otherwise the meta-skill's first step (e.g.
    multi-search-engine reading {{ inputs.user_message }}) gets an empty
    query and the whole DAG produces an empty deliverable.

    The Agent stores message in self._current_turn_message at the top of
    _turn_generator; _run_one_streaming reads it back from there. This test
    sets the attribute directly (without going through run_turn) and
    verifies the value reaches MetaOrchestrator via a captured iter_events
    spy.
    """
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.tool_boundary import ToolCall, ToolResult
    from opensquilla.tools.builtin import meta_tools  # noqa: F401
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext

    bundled = tmp_path / "skills" / "bundled"
    bundled.mkdir(parents=True)
    skill_dir = bundled / "meta-tiny"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: meta-tiny\n"
        "kind: meta\n"
        "description: t\n"
        "triggers: [t]\n"
        "composition:\n"
        "  steps:\n"
        "    - id: c\n"
        "      kind: llm_classify\n"
        "      output_choices: [A, B]\n"
        "      with: {text: x}\n"
        "---\n# meta-tiny\n",
        encoding="utf-8",
    )
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    loader.load_all()

    class _NullProvider:
        provider_name = "null"

        async def chat(self, *_a, **_kw):
            raise AssertionError("provider.chat must not fire")

        async def list_models(self):
            return []

    registry = get_default_registry()
    config = AgentConfig(
        model_id="stub", max_iterations=1, system_prompt="outer system prompt",
        metadata={"skill_loader": loader, "bootstrap_workspace_dir": str(tmp_path)},
    )
    agent = Agent(
        provider=_NullProvider(),  # type: ignore[arg-type]
        config=config,
        tool_definitions=[],
        tool_handler=None,
        tool_registry=registry,
    )
    # Simulate what _turn_generator does on its first line.
    agent._current_turn_message = "RAG in low-resource settings"  # type: ignore[attr-defined]

    captured: dict[str, object] = {}

    # Patch MetaOrchestrator.iter_events to capture the MetaMatch then
    # yield a successful MetaResult sentinel without running real steps.
    import opensquilla.skills.meta.orchestrator as orch_mod
    from opensquilla.skills.meta.types import MetaResult

    original_iter_events = orch_mod.MetaOrchestrator.iter_events

    async def fake_iter_events(self, match):  # noqa: ARG001
        captured["inputs"] = dict(match.inputs)
        yield MetaResult(ok=True, final_text="captured")

    orch_mod.MetaOrchestrator.iter_events = fake_iter_events  # type: ignore[assignment]
    try:
        tc = ToolCall(
            tool_use_id="u1", tool_name="meta_invoke",
            arguments={"name": "meta-tiny"},
        )
        tool_ctx = ToolContext(workspace_dir=str(tmp_path), is_owner=True)

        final: ToolResult | None = None
        async for ev in agent._run_one_streaming(tc, tool_ctx):
            if isinstance(ev, ToolResult):
                final = ev
    finally:
        orch_mod.MetaOrchestrator.iter_events = original_iter_events  # type: ignore[assignment]

    assert final is not None
    assert final.is_error is False
    assert final.content == "meta-skill 'meta-tiny' completed."
    assert captured.get("inputs", {}).get("user_message") == "RAG in low-resource settings", (
        f"expected user_message to propagate from _current_turn_message; got {captured!r}"
    )
    assert captured.get("inputs", {}).get("system_prompt") == "outer system prompt", (
        f"expected system_prompt to propagate into meta-skill inputs; got {captured!r}"
    )


@pytest.mark.asyncio
async def test_run_one_streaming_reuses_resolved_meta_match_control_inputs(
    tmp_path,
) -> None:
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.skills.meta.parser import parse_meta_plan
    from opensquilla.skills.meta.types import MetaMatch, MetaResult
    from opensquilla.tool_boundary import ToolCall, ToolResult
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext

    bundled = tmp_path / "skills" / "bundled"
    bundled.mkdir(parents=True)
    skill_dir = bundled / "meta-tiny"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: meta-tiny\n"
        "kind: meta\n"
        "description: t\n"
        "triggers: [t]\n"
        "composition:\n"
        "  request:\n"
        "    mode: confirm\n"
        "    fields:\n"
        "      - name: audience\n"
        "        required: true\n"
        "  steps:\n"
        "    - id: c\n"
        "      kind: llm_classify\n"
        "      output_choices: [A, B]\n"
        "      with: {text: x}\n"
        "---\n# meta-tiny\n",
        encoding="utf-8",
    )
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    specs = loader.load_all()
    plan = parse_meta_plan(next(spec for spec in specs if spec.name == "meta-tiny"))
    assert plan is not None
    resolved = MetaMatch(
        plan=plan,
        inputs={
            "user_message": "Visible request only",
            "audience": "decision owner",
            "meta_preflight_confirmed": True,
            "meta_preflight_run_id": "01CONTROL",
        },
        run_id="01CONTROL",
    )

    class _NullProvider:
        provider_name = "null"

        async def chat(self, *_a, **_kw):
            raise AssertionError("provider.chat must not fire")

        async def list_models(self):
            return []

    agent = Agent(
        provider=_NullProvider(),  # type: ignore[arg-type]
        config=AgentConfig(
            model_id="stub",
            max_iterations=1,
            system_prompt="outer system prompt",
            metadata={
                "skill_loader": loader,
                "bootstrap_workspace_dir": str(tmp_path),
                "meta_match": resolved,
            },
        ),
        tool_definitions=[],
        tool_handler=None,
        tool_registry=get_default_registry(),
    )
    agent._current_turn_message = "Visible request only"  # type: ignore[attr-defined]

    captured: dict[str, object] = {}
    import opensquilla.skills.meta.orchestrator as orch_mod

    original_iter_events = orch_mod.MetaOrchestrator.iter_events

    async def fake_iter_events(self, match):  # noqa: ARG001
        captured["inputs"] = dict(match.inputs)
        captured["run_id"] = match.run_id
        yield MetaResult(ok=True, final_text="captured")

    orch_mod.MetaOrchestrator.iter_events = fake_iter_events  # type: ignore[assignment]
    try:
        tc = ToolCall(
            tool_use_id="u1",
            tool_name="meta_invoke",
            arguments={"name": "meta-tiny"},
        )
        tool_ctx = ToolContext(workspace_dir=str(tmp_path), is_owner=True)

        final: ToolResult | None = None
        async for ev in agent._run_one_streaming(tc, tool_ctx):
            if isinstance(ev, ToolResult):
                final = ev
    finally:
        orch_mod.MetaOrchestrator.iter_events = original_iter_events  # type: ignore[assignment]

    assert final is not None
    assert final.is_error is False
    assert captured["run_id"] == "01CONTROL"
    assert captured["inputs"] == {
        "user_message": "Visible request only",
        "audience": "decision owner",
        "meta_preflight_confirmed": True,
        "meta_preflight_run_id": "01CONTROL",
        "system_prompt": "outer system prompt",
    }


# ---------------------------------------------------------------------------
# Task 6: Dispatch loop intercepts meta_invoke and terminates turn on success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_intercepts_meta_invoke_and_terminates_turn(
    tmp_path,
) -> None:
    """When the LLM emits tool_use(meta_invoke, ...), the dispatch loop
    must intercept BEFORE the standard handler (which would raise
    RuntimeError from the Task 1 guard) and call _run_one_streaming
    inline. On success, terminates_turn=True must propagate to the
    Agent's turn_yielded flag so the outer loop exits."""
    from collections.abc import AsyncIterator

    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import (
        AgentConfig,
        DoneEvent,
        ErrorEvent,
        TextDeltaEvent,
        ToolResultEvent,
    )
    from opensquilla.provider.types import (
        DoneEvent as ProviderDoneEvent,
    )
    from opensquilla.provider.types import (
        ToolUseDeltaEvent as ProviderToolUseDelta,
    )
    from opensquilla.provider.types import (
        ToolUseEndEvent as ProviderToolUseEnd,
    )
    from opensquilla.provider.types import (
        ToolUseStartEvent as ProviderToolUseStart,
    )
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.tools.builtin import meta_tools  # noqa: F401 — registers meta_invoke
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext

    # Synthesize a tiny meta-skill (same trick as Task 5 happy path).
    bundled = tmp_path / "skills" / "bundled"
    bundled.mkdir(parents=True)
    skill_dir = bundled / "meta-tiny"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: meta-tiny\n"
        "kind: meta\n"
        "description: tiny meta-skill for dispatch test\n"
        "triggers: [tiny-meta-trigger]\n"
        "composition:\n"
        "  steps:\n"
        "    - id: c\n"
        "      kind: llm_classify\n"
        "      output_choices: [A, B]\n"
        "      with: {text: \"x\"}\n"
        "---\n"
        "# meta-tiny\n",
        encoding="utf-8",
    )
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    loader.load_all()

    # Stub provider that emits ONE tool_use(meta_invoke, name="meta-tiny")
    # then DoneEvent. If the dispatch loop ever lets the meta_invoke
    # tool reach the standard handler, the registered guard raises
    # RuntimeError and the turn ends with an error.
    class _StubProvider:
        provider_name = "stub"

        async def chat(
            self, messages, tools=None, config=None,
        ) -> AsyncIterator:
            yield ProviderToolUseStart(
                tool_use_id="tu_1",
                tool_name="meta_invoke",
            )
            yield ProviderToolUseDelta(
                tool_use_id="tu_1",
                json_fragment='{"name": "meta-tiny"}',
            )
            yield ProviderToolUseEnd(
                tool_use_id="tu_1",
                tool_name="meta_invoke",
                arguments={"name": "meta-tiny"},
            )
            yield ProviderDoneEvent(stop_reason="tool_use")

        async def list_models(self):
            return []

    registry = get_default_registry()
    assert registry.get("meta_invoke") is not None

    config = AgentConfig(
        model_id="stub",
        max_iterations=4,
        system_prompt="",
        metadata={"skill_loader": loader, "bootstrap_workspace_dir": str(tmp_path)},
    )

    agent = Agent(
        provider=_StubProvider(),  # type: ignore[arg-type]
        config=config,
        tool_definitions=[],
        tool_handler=None,
        tool_registry=registry,
        tool_context=ToolContext(workspace_dir=str(tmp_path), is_owner=True),
    )

    # Override the llm_classify path
    async def fake_llm_chat(_s: str, _u: str) -> str:
        return "A"

    agent._test_llm_chat_override = fake_llm_chat  # type: ignore[attr-defined]

    # Drive the turn
    events = []
    async for ev in agent.run_turn("trigger meta-tiny somehow"):
        events.append(ev)

    # The Task 1 guard handler raises a RuntimeError that mentions
    # "_run_one_streaming" or "intercept". If interception failed and
    # the handler was hit, that error would surface in the
    # ToolResultEvent emitted afterward. Search for it.
    error_texts: list[str] = []
    for e in events:
        if isinstance(e, ToolResultEvent):
            error_texts.append(e.result or "")
        if isinstance(e, ErrorEvent):
            error_texts.append(e.message or "")
    flat = " | ".join(error_texts)
    assert "_run_one_streaming" not in flat, (
        f"Dispatch loop did NOT intercept meta_invoke — guard handler "
        f"was reached. Events: {flat[:500]}"
    )

    # Turn must terminate cleanly with a DoneEvent (terminates_turn drives
    # the outer-loop break, after which the agent emits DoneEvent).
    assert any(isinstance(e, DoneEvent) for e in events), (
        "Expected DoneEvent at end of turn"
    )

    # And critically — the ToolResultEvent for meta_invoke must show
    # is_error=False (success path). If interception failed, the
    # standard handler would have raised RuntimeError and the
    # ToolResultEvent would carry is_error=True.
    meta_invoke_results = [
        e for e in events
        if isinstance(e, ToolResultEvent) and e.tool_name == "meta_invoke"
    ]
    assert meta_invoke_results, "Expected at least one ToolResultEvent for meta_invoke"
    assert all(not r.is_error for r in meta_invoke_results), (
        f"meta_invoke ToolResultEvent must be success; got error contents: "
        f"{[r.result for r in meta_invoke_results if r.is_error]}"
    )

    # Positive evidence: the meta-skill's single llm_classify step is
    # overridden to return "A" (see fake_llm_chat). On success that text
    # must surface in the meta_invoke ToolResultEvent content — proves
    # the orchestrator actually ran the composition, not just that the
    # dispatch interceptor silently returned an empty success.
    streamed_text = "".join(e.text for e in events if isinstance(e, TextDeltaEvent))
    assert "A" in streamed_text, (
        "Expected llm_classify result 'A' to stream as final answer; "
        f"got: {streamed_text[:300]!r}"
    )
    success_contents = " | ".join(r.result or "" for r in meta_invoke_results)
    assert "meta-skill 'meta-tiny' completed." in success_contents

    # The orchestrator emits ToolUseStartEvent / ToolResultEvent for each
    # meta-step (tool_name="meta-step:<step_id>"). The dispatch interceptor
    # must forward these to the outer turn stream so the WebUI can render
    # each step as a tool-call card — same visual treatment as the
    # hard-takeover path. If these don't appear, soft-path turns look
    # like a single opaque "meta_invoke" tool call to the UI, even though
    # internally a multi-step DAG ran.
    from opensquilla.engine.types import ToolUseStartEvent
    step_starts = [
        e for e in events
        if isinstance(e, ToolUseStartEvent) and e.tool_name.startswith("meta-step:")
    ]
    step_results = [
        e for e in events
        if isinstance(e, ToolResultEvent) and e.tool_name.startswith("meta-step:")
    ]
    assert step_starts, (
        "Expected at least one ToolUseStartEvent with tool_name='meta-step:<id>' "
        "in the parent turn stream — dispatch interceptor must forward nested "
        f"orchestrator events. Got event types: "
        f"{sorted({type(e).__name__ for e in events})}"
    )
    assert step_results, (
        "Expected at least one ToolResultEvent with tool_name='meta-step:<id>'."
    )


@pytest.mark.asyncio
async def test_dispatch_coerces_meta_skill_view_to_meta_invoke(
    tmp_path,
) -> None:
    """If the model calls skill_view for a meta-skill, treat it as
    meta_invoke so reading the meta SKILL.md cannot silently bypass the
    orchestrator."""
    from collections.abc import AsyncIterator

    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig, DoneEvent, TextDeltaEvent, ToolResultEvent
    from opensquilla.provider.types import DoneEvent as ProviderDoneEvent
    from opensquilla.provider.types import ToolUseDeltaEvent as ProviderToolUseDelta
    from opensquilla.provider.types import ToolUseEndEvent as ProviderToolUseEnd
    from opensquilla.provider.types import ToolUseStartEvent as ProviderToolUseStart
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.tools.builtin import meta_tools  # noqa: F401
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext

    bundled = tmp_path / "skills" / "bundled"
    skill_dir = bundled / "meta-tiny"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: meta-tiny\n"
        "kind: meta\n"
        "description: tiny meta-skill for skill_view coercion\n"
        "triggers: [tiny-meta-trigger]\n"
        "composition:\n"
        "  steps:\n"
        "    - id: c\n"
        "      kind: llm_classify\n"
        "      output_choices: [A, B]\n"
        "      with: {text: \"x\"}\n"
        "---\n"
        "# meta-tiny\n",
        encoding="utf-8",
    )
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    loader.load_all()

    class _StubProvider:
        provider_name = "stub"

        async def chat(
            self, messages, tools=None, config=None,
        ) -> AsyncIterator:
            yield ProviderToolUseStart(tool_use_id="tu_1", tool_name="skill_view")
            yield ProviderToolUseDelta(
                tool_use_id="tu_1",
                json_fragment='{"name": "meta-tiny"}',
            )
            yield ProviderToolUseEnd(
                tool_use_id="tu_1",
                tool_name="skill_view",
                arguments={"name": "meta-tiny"},
            )
            yield ProviderDoneEvent(stop_reason="tool_use")

        async def list_models(self):
            return []

    agent = Agent(
        provider=_StubProvider(),  # type: ignore[arg-type]
        config=AgentConfig(
            model_id="stub",
            max_iterations=4,
            system_prompt="",
            metadata={
                "skill_loader": loader,
                "bootstrap_workspace_dir": str(tmp_path),
            },
        ),
        tool_definitions=[],
        tool_handler=None,
        tool_registry=get_default_registry(),
        tool_context=ToolContext(workspace_dir=str(tmp_path), is_owner=True),
    )

    async def fake_llm_chat(_s: str, _u: str) -> str:
        return "A"

    agent._test_llm_chat_override = fake_llm_chat  # type: ignore[attr-defined]

    events = []
    async for ev in agent.run_turn("trigger meta-tiny via skill_view"):
        events.append(ev)

    assert any(isinstance(e, DoneEvent) for e in events)
    meta_invoke_results = [
        e for e in events
        if isinstance(e, ToolResultEvent) and e.tool_name == "meta_invoke"
    ]
    assert meta_invoke_results, (
        "skill_view(name=<meta-skill>) must be coerced into meta_invoke"
    )
    assert all(not r.is_error for r in meta_invoke_results)
    assert any(
        "meta-skill 'meta-tiny' completed." in (r.result or "")
        for r in meta_invoke_results
    )
    assert "A" in "".join(e.text for e in events if isinstance(e, TextDeltaEvent))


@pytest.mark.asyncio
async def test_dispatch_repairs_malformed_meta_invoke_from_matched_meta_skill(
    tmp_path,
) -> None:
    """A deterministic meta match may force ``meta_invoke`` on small models
    that emit raw/non-JSON arguments. Repair that to the matched skill name
    instead of letting dispatch reject the tool call."""
    from collections.abc import AsyncIterator
    from types import SimpleNamespace

    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig, DoneEvent, TextDeltaEvent, ToolResultEvent
    from opensquilla.provider.types import DoneEvent as ProviderDoneEvent
    from opensquilla.provider.types import ToolUseEndEvent as ProviderToolUseEnd
    from opensquilla.provider.types import ToolUseStartEvent as ProviderToolUseStart
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.tools.builtin import meta_tools  # noqa: F401
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext

    bundled = tmp_path / "skills" / "bundled"
    skill_dir = bundled / "meta-tiny"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: meta-tiny\n"
        "kind: meta\n"
        "description: tiny meta-skill for malformed meta_invoke coercion\n"
        "triggers: [tiny-meta-trigger]\n"
        "composition:\n"
        "  steps:\n"
        "    - id: c\n"
        "      kind: llm_classify\n"
        "      output_choices: [A, B]\n"
        "      with: {text: \"x\"}\n"
        "---\n",
        encoding="utf-8",
    )
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    loader.load_all()

    class _StubProvider:
        provider_name = "stub"

        async def chat(self, messages, tools=None, config=None) -> AsyncIterator:
            yield ProviderToolUseStart(tool_use_id="tu_1", tool_name="meta_invoke")
            yield ProviderToolUseEnd(
                tool_use_id="tu_1",
                tool_name="meta_invoke",
                arguments={"_raw": "meta-tiny"},
            )
            yield ProviderDoneEvent(stop_reason="tool_use")

        async def list_models(self):
            return []

    agent = Agent(
        provider=_StubProvider(),  # type: ignore[arg-type]
        config=AgentConfig(
            model_id="stub",
            max_iterations=4,
            system_prompt="",
            metadata={
                "skill_loader": loader,
                "bootstrap_workspace_dir": str(tmp_path),
                "meta_match": SimpleNamespace(
                    plan=SimpleNamespace(name="meta-tiny"),
                ),
                "meta_match_tool_choice": {
                    "type": "function",
                    "function": {"name": "meta_invoke"},
                },
            },
        ),
        tool_definitions=[],
        tool_handler=None,
        tool_registry=get_default_registry(),
        tool_context=ToolContext(workspace_dir=str(tmp_path), is_owner=True),
    )

    async def fake_llm_chat(_s: str, _u: str) -> str:
        return "A"

    agent._test_llm_chat_override = fake_llm_chat  # type: ignore[attr-defined]

    events = [ev async for ev in agent.run_turn("tiny-meta-trigger")]

    assert any(isinstance(e, DoneEvent) for e in events)
    meta_invoke_results = [
        e for e in events
        if isinstance(e, ToolResultEvent) and e.tool_name == "meta_invoke"
    ]
    assert meta_invoke_results
    assert all(not r.is_error for r in meta_invoke_results)
    assert "A" in "".join(e.text for e in events if isinstance(e, TextDeltaEvent))


@pytest.mark.asyncio
async def test_dispatch_rewrites_other_tool_after_forced_meta_match(
    tmp_path,
) -> None:
    """If a forced deterministic meta match is present, do not let an ordinary
    tool call bypass the matched meta DAG."""
    from collections.abc import AsyncIterator
    from types import SimpleNamespace

    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig, ToolResultEvent
    from opensquilla.provider.types import DoneEvent as ProviderDoneEvent
    from opensquilla.provider.types import ToolUseEndEvent as ProviderToolUseEnd
    from opensquilla.provider.types import ToolUseStartEvent as ProviderToolUseStart
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.tools.builtin import meta_tools  # noqa: F401
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext

    bundled = tmp_path / "skills" / "bundled"
    skill_dir = bundled / "meta-tiny"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: meta-tiny\n"
        "kind: meta\n"
        "description: tiny meta-skill for forced rewrite\n"
        "triggers: [tiny-meta-trigger]\n"
        "composition:\n"
        "  steps:\n"
        "    - id: c\n"
        "      kind: llm_classify\n"
        "      output_choices: [A, B]\n"
        "      with: {text: \"x\"}\n"
        "---\n",
        encoding="utf-8",
    )
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    loader.load_all()

    class _StubProvider:
        provider_name = "stub"

        async def chat(self, messages, tools=None, config=None) -> AsyncIterator:
            yield ProviderToolUseStart(tool_use_id="tu_1", tool_name="memory_search")
            yield ProviderToolUseEnd(
                tool_use_id="tu_1",
                tool_name="memory_search",
                arguments={"query": "x"},
            )
            yield ProviderDoneEvent(stop_reason="tool_use")

        async def list_models(self):
            return []

    agent = Agent(
        provider=_StubProvider(),  # type: ignore[arg-type]
        config=AgentConfig(
            model_id="stub",
            max_iterations=4,
            system_prompt="",
            metadata={
                "skill_loader": loader,
                "bootstrap_workspace_dir": str(tmp_path),
                "meta_match": SimpleNamespace(
                    plan=SimpleNamespace(name="meta-tiny"),
                ),
                "meta_match_tool_choice": {
                    "type": "function",
                    "function": {"name": "meta_invoke"},
                },
            },
        ),
        tool_definitions=[],
        tool_handler=None,
        tool_registry=get_default_registry(),
        tool_context=ToolContext(workspace_dir=str(tmp_path), is_owner=True),
    )

    async def fake_llm_chat(_s: str, _u: str) -> str:
        return "A"

    agent._test_llm_chat_override = fake_llm_chat  # type: ignore[attr-defined]

    events = [ev async for ev in agent.run_turn("tiny-meta-trigger")]
    meta_invoke_results = [
        e for e in events
        if isinstance(e, ToolResultEvent) and e.tool_name == "meta_invoke"
    ]

    assert meta_invoke_results
    assert all(not r.is_error for r in meta_invoke_results)
    assert not any(
        isinstance(e, ToolResultEvent) and e.tool_name == "memory_search"
        for e in events
    )


# ---------------------------------------------------------------------------
# Task 5C: Soft path wires meta_run_writer + triggered_by="soft_meta_invoke"
# into the MetaOrchestrator ctor when AgentConfig.metadata carries the writer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meta_invoke_passes_writer_with_soft_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Soft path constructs MetaOrchestrator with triggered_by='soft_meta_invoke'
    and forwards the writer from AgentConfig.metadata['meta_run_writer']."""
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.types import AgentConfig
    from opensquilla.persistence.meta_run_writer import open_meta_run_writer
    from opensquilla.persistence.migrator import apply_pending
    from opensquilla.skills.loader import SkillLoader
    from opensquilla.skills.meta.types import MetaResult
    from opensquilla.tool_boundary import ToolCall, ToolResult
    from opensquilla.tools.builtin import meta_tools  # noqa: F401 — registers meta_invoke
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import ToolContext

    # Apply migrations + open writer against tmp_path DB.
    db = str(tmp_path / "t.db")
    migrations_dir = Path(__file__).resolve().parents[1].parent / "migrations"
    apply_pending(db, migrations_dir)
    writer = open_meta_run_writer(db)

    # Synthesize a tiny meta-skill so plan parsing succeeds.
    bundled = tmp_path / "skills" / "bundled"
    bundled.mkdir(parents=True)
    skill_dir = bundled / "meta-tiny"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: meta-tiny\n"
        "kind: meta\n"
        "description: t\n"
        "triggers: [t]\n"
        "composition:\n"
        "  steps:\n"
        "    - id: c\n"
        "      kind: llm_classify\n"
        "      output_choices: [A, B]\n"
        "      with: {text: x}\n"
        "---\n# meta-tiny\n",
        encoding="utf-8",
    )
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    loader.load_all()

    class _NullProvider:
        provider_name = "null"

        async def chat(self, *_a, **_kw):
            raise AssertionError("provider.chat must not fire")

        async def list_models(self):
            return []

    registry = get_default_registry()

    config = AgentConfig(
        model_id="stub",
        max_iterations=1,
        system_prompt="",
        metadata={
            "skill_loader": loader,
            "bootstrap_workspace_dir": str(tmp_path),
            "meta_run_writer": writer,
        },
    )
    agent = Agent(
        provider=_NullProvider(),  # type: ignore[arg-type]
        config=config,
        tool_definitions=[],
        tool_handler=None,
        tool_registry=registry,
        session_key="sess-soft-1",
    )

    captured: dict[str, object] = {}

    class _StubOrch:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        async def iter_events(self, _match):
            yield MetaResult(ok=True, final_text="captured")

    monkeypatch.setattr(
        "opensquilla.skills.meta.orchestrator.MetaOrchestrator",
        _StubOrch,
    )

    tc = ToolCall(
        tool_use_id="u1",
        tool_name="meta_invoke",
        arguments={"name": "meta-tiny"},
    )
    tool_ctx = ToolContext(workspace_dir=str(tmp_path), is_owner=True)

    final: ToolResult | None = None
    async for ev in agent._run_one_streaming(tc, tool_ctx):
        if isinstance(ev, ToolResult):
            final = ev

    try:
        assert final is not None
        assert final.is_error is False
        assert captured.get("triggered_by") == "soft_meta_invoke"
        assert captured.get("run_writer") is writer
        assert captured.get("session_key") == "sess-soft-1"
    finally:
        writer.close()
