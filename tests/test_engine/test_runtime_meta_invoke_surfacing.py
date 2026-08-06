"""TurnRunner._build_tools surfaces meta_invoke when meta-skills are loaded.

meta_invoke is registered with ``exposed_by_default=False`` so the tool
catalogue stays clean in deployments that don't ship meta-skills. When
at least one ``kind=meta`` skill IS loaded, ``_build_tools`` must add
``"meta_invoke"`` to ``ctx.surfaced_tools`` so the registry's visibility
check at :func:`ToolRegistry._is_visible` lets it through.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.runtime import TurnRunner
from opensquilla.provider import ToolDefinition, ToolInputSchema
from opensquilla.skills.capability_runtime import (
    META_CAPABILITY_API_KEY_ENV,
    META_CAPABILITY_BASE_URL_ENV,
    META_CAPABILITY_PROVIDER_ENV,
)
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.meta.readiness import (
    META_OPENROUTER_API_KEY_ENV,
    META_READINESS_ENV_ALIASES_METADATA_KEY,
    META_SKILL_RUNTIME_ENV_PROVIDER_METADATA_KEY,
)
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import ToolContext


def _make_loader_with_meta(
    tmp_path: Path,
    *,
    disable_model_invocation: bool = False,
) -> SkillLoader:
    bundled = tmp_path / "skills" / "bundled"
    bundled.mkdir(parents=True)
    skill_dir = bundled / "meta-tiny"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        '---\n'
        'name: meta-tiny\n'
        'kind: meta\n'
        'description: tiny meta-skill\n'
        f'disable-model-invocation: {str(disable_model_invocation).lower()}\n'
        'triggers: [tiny-meta-trigger]\n'
        'composition:\n'
        '  steps:\n'
        '    - id: c\n'
        '      kind: llm_classify\n'
        '      output_choices: [A, B]\n'
        '      with: {text: "x"}\n'
        '---\n'
        '# meta-tiny\n',
        encoding="utf-8",
    )
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    loader.load_all()
    return loader


def _make_loader_without_meta(tmp_path: Path) -> SkillLoader:
    bundled = tmp_path / "skills" / "bundled"
    bundled.mkdir(parents=True)
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snap.json")
    loader.invalidate_cache()
    loader.load_all()
    return loader


def test_build_tools_surfaces_meta_invoke_when_meta_skill_present(
    tmp_path: Path,
) -> None:
    registry = get_default_registry()
    loader = _make_loader_with_meta(tmp_path)
    runner = TurnRunner(provider_selector=None, config=_meta_cfg(auto_trigger=True))
    runner._tool_registry = registry
    runner._skill_loader = loader

    ctx = ToolContext(is_owner=True, workspace_dir=str(tmp_path))
    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(td, "name", "") for td in tool_defs}

    assert "meta_invoke" in names, (
        f"meta_invoke should be surfaced when meta-skills present; got {sorted(names)[:20]}"
    )
    assert ctx.surfaced_tools is not None
    assert "meta_invoke" in ctx.surfaced_tools


def test_build_tools_does_not_surface_meta_invoke_without_meta_skills(
    tmp_path: Path,
) -> None:
    """When no meta-skills are loaded, meta_invoke stays hidden — its
    ``exposed_by_default=False`` keeps the catalogue tight for deployments
    that don't ship meta-skills."""
    registry = get_default_registry()
    loader = _make_loader_without_meta(tmp_path)
    runner = TurnRunner(provider_selector=None, config=None)
    runner._tool_registry = registry
    runner._skill_loader = loader

    ctx = ToolContext(is_owner=True, workspace_dir=str(tmp_path))
    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(td, "name", "") for td in tool_defs}

    assert "meta_invoke" not in names, (
        f"meta_invoke should be hidden when no meta-skills present; got {sorted(names)[:20]}"
    )
    # surfaced_tools may stay None (no mutation) — either way meta_invoke
    # must not be inside it
    assert ctx.surfaced_tools is None or "meta_invoke" not in ctx.surfaced_tools


def test_build_tools_does_not_surface_meta_invoke_for_disabled_meta_skill(
    tmp_path: Path,
) -> None:
    registry = get_default_registry()
    loader = _make_loader_with_meta(tmp_path, disable_model_invocation=True)
    runner = TurnRunner(provider_selector=None, config=None)
    runner._tool_registry = registry
    runner._skill_loader = loader

    ctx = ToolContext(is_owner=True, workspace_dir=str(tmp_path))
    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(td, "name", "") for td in tool_defs}

    assert "meta_invoke" not in names
    assert ctx.surfaced_tools is None or "meta_invoke" not in ctx.surfaced_tools


def test_build_tools_does_not_surface_meta_invoke_when_meta_skill_disabled_by_config(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    registry = get_default_registry()
    loader = _make_loader_with_meta(tmp_path)
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(meta_skill=SimpleNamespace(enabled=False)),
    )
    runner._tool_registry = registry
    runner._skill_loader = loader

    ctx = ToolContext(is_owner=True, workspace_dir=str(tmp_path))
    metadata: dict[str, object] = {}
    tool_defs, _handler = runner._build_tools(ctx, metadata=metadata)
    names = {getattr(td, "name", "") for td in tool_defs}

    assert "meta_invoke" not in names
    assert ctx.surfaced_tools is None or "meta_invoke" not in ctx.surfaced_tools
    assert metadata["meta_skill_enabled"] is False


def test_build_tools_preserves_existing_surfaced_tools(tmp_path: Path) -> None:
    """If the caller pre-populates ctx.surfaced_tools (e.g. for a custom
    per-request tool surface), _build_tools must add to it, not overwrite."""
    registry = get_default_registry()
    loader = _make_loader_with_meta(tmp_path)
    runner = TurnRunner(provider_selector=None, config=_meta_cfg(auto_trigger=True))
    runner._tool_registry = registry
    runner._skill_loader = loader

    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        surfaced_tools={"some_other_tool"},
    )
    runner._build_tools(ctx)
    assert ctx.surfaced_tools is not None
    assert "meta_invoke" in ctx.surfaced_tools
    assert "some_other_tool" in ctx.surfaced_tools, (
        "must extend existing surfaced_tools, not replace it"
    )


def test_runtime_does_not_hard_auto_invoke_meta_match() -> None:
    """Meta trigger matches must go through the outer LLM prompt/tool path.

    The meta_resolution step already injects a system-prompt hint and exposes
    meta_invoke. Runtime must not bypass that prompt by directly calling
    _run_one_streaming when metadata["meta_match"] is present.
    """
    source = inspect.getsource(TurnRunner._run_turn)

    assert "meta_resolution.auto_invoke" not in source
    assert "auto_meta_invoke_" not in source


@pytest.mark.asyncio
async def test_runtime_pipeline_runs_meta_resolution_before_skill_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def noop_router(ctx: TurnContext) -> TurnContext:
        return ctx

    noop_router.__name__ = "apply_squilla_router"
    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", noop_router)

    loader = _make_loader_with_meta(tmp_path)
    runner = TurnRunner(provider_selector=None, config=_meta_cfg(auto_trigger=True))
    runner._skill_loader = loader

    turn, _provider = await runner._run_pipeline(
        "please run tiny-meta-trigger for this request",
        "agent:main:test-meta-resolution",
        None,
        None,
        [
            ToolDefinition(
                name="meta_invoke",
                description="invoke meta skills",
                input_schema=ToolInputSchema(),
            ),
            ToolDefinition(
                name="web_search",
                description="search the web",
                input_schema=ToolInputSchema(),
            ),
        ],
        "base prompt",
        [],
    )

    step_names = [record.step_name for record in turn.metadata["pipeline_steps"]]
    assert step_names.index("meta_resolution") < step_names.index("filter_skills")
    assert turn.metadata["meta_match"].plan.name == "meta-tiny"
    assert "meta_invoke(name=\"meta-tiny\")" in str(turn.system_prompt)
    assert "meta-tiny" in str(turn.system_prompt)

    # Deterministic trigger matches force the first tool call to meta_invoke so
    # cheaper routed models do not bypass the meta DAG by calling ordinary tools.
    assert {tool.name for tool in turn.tool_defs} == {"meta_invoke", "web_search"}
    assert "meta_match_tool_surface_restricted" not in turn.metadata
    assert turn.metadata["meta_match_tool_choice"] == {
        "type": "function",
        "function": {"name": "meta_invoke"},
    }


@pytest.mark.asyncio
async def test_runtime_pipeline_restores_mainline_meta_and_coding_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def noop_router(ctx: TurnContext) -> TurnContext:
        return ctx

    noop_router.__name__ = "apply_squilla_router"
    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", noop_router)

    runner = TurnRunner(provider_selector=None, config=_meta_cfg(auto_trigger=False))
    runner._skill_loader = _make_loader_without_meta(tmp_path)

    turn, _provider = await runner._run_pipeline(
        "hello",
        "agent:main:test-mainline-pipeline-order",
        None,
        None,
        [
            ToolDefinition(
                name="web_search",
                description="search the web",
                input_schema=ToolInputSchema(),
            ),
        ],
        "base prompt",
        [],
    )

    assert [record.step_name for record in turn.metadata["pipeline_steps"]] == [
        "resolve_model",
        "apply_vision_followup_gate",
        "apply_squilla_router",
        "observe_reasoning_hint",
        "meta_resolution",
        "enforce_coding_mode",
        "meta_command_launch",
        "filter_skills",
        "inject_subagent_grounding",
        "inject_platform_hint",
        "apply_prompt_cache",
    ]


@pytest.mark.asyncio
async def test_runtime_pipeline_pins_meta_skill_when_skill_filter_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the retriever is on, ``<available_skills>`` may drop the
    meta-skill description out of the prompt. The matched meta-skill is
    pinned, and deterministic trigger matches force meta_invoke as the first
    tool call while leaving the broader tool surface intact for later turns.
    """
    async def noop_router(ctx: TurnContext) -> TurnContext:
        return ctx

    noop_router.__name__ = "apply_squilla_router"
    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", noop_router)

    loader = _make_loader_with_meta(tmp_path)
    skills_cfg = SimpleNamespace(
        filter_enabled=True,
        filter_top_k=5,
        max_skills_prompt_chars=8000,
        injection_mode="system",
    )
    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            skills=skills_cfg,
            meta_skill=SimpleNamespace(enabled=True, auto_trigger=True),
        ),
    )
    runner._skill_loader = loader

    turn, _provider = await runner._run_pipeline(
        "please run tiny-meta-trigger for this request",
        "agent:main:test-meta-restrict",
        None,
        None,
        [
            ToolDefinition(
                name="meta_invoke",
                description="invoke meta skills",
                input_schema=ToolInputSchema(),
            ),
            ToolDefinition(
                name="web_search",
                description="search the web",
                input_schema=ToolInputSchema(),
            ),
        ],
        "base prompt",
        [],
    )

    assert turn.metadata["meta_match"].plan.name == "meta-tiny"
    assert {tool.name for tool in turn.tool_defs} == {"meta_invoke", "web_search"}
    assert "meta_match_tool_surface_restricted" not in turn.metadata
    assert turn.metadata["meta_match_tool_choice"] == {
        "type": "function",
        "function": {"name": "meta_invoke"},
    }
    assert "meta-tiny" in str(turn.system_prompt)


def _meta_cfg(auto_trigger: bool) -> SimpleNamespace:
    return SimpleNamespace(meta_skill=SimpleNamespace(enabled=True, auto_trigger=auto_trigger))


@pytest.mark.asyncio
async def test_pipeline_projects_configured_openrouter_key_as_name_only_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def noop_router(ctx: TurnContext) -> TurnContext:
        return ctx

    noop_router.__name__ = "apply_squilla_router"
    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", noop_router)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    secret = "synthetic-openrouter-runtime-key"
    config = SimpleNamespace(
        meta_skill=SimpleNamespace(enabled=True, auto_trigger=False),
        llm=SimpleNamespace(
            provider="openrouter",
            api_key=secret,
            api_key_env="",
        ),
    )
    runner = TurnRunner(provider_selector=None, config=config)
    trusted_loader = SkillLoader(
        bundled_dir=(
            Path(__file__).resolve().parents[2]
            / "src"
            / "opensquilla"
            / "skills"
            / "bundled"
        ),
        snapshot_path=tmp_path / "trusted-snapshot.json",
    )
    runner._skill_loader = trusted_loader

    turn, _provider = await runner._run_pipeline(
        "hello",
        "agent:main:test-openrouter-readiness-alias",
        None,
        None,
        [ToolDefinition(name="web_search", description="search", input_schema=ToolInputSchema())],
        "base prompt",
        [],
    )

    parent_spec = trusted_loader.get_by_name("meta-short-drama")
    assert parent_spec is not None
    plan = parse_meta_plan(parent_spec)
    assert plan is not None

    readiness_alias_provider = turn.metadata[
        META_READINESS_ENV_ALIASES_METADATA_KEY
    ]
    assert callable(readiness_alias_provider)
    assert readiness_alias_provider(parent_spec, plan) == ("OPENROUTER_API_KEY",)
    assert secret not in repr(turn.metadata)
    runtime_env_provider = turn.metadata[
        META_SKILL_RUNTIME_ENV_PROVIDER_METADATA_KEY
    ]
    assert callable(runtime_env_provider)
    expected_connection = {
        META_CAPABILITY_PROVIDER_ENV: "openrouter",
        META_CAPABILITY_API_KEY_ENV: secret,
        META_CAPABILITY_BASE_URL_ENV: "https://openrouter.ai/api/v1",
        META_OPENROUTER_API_KEY_ENV: secret,
    }
    assert runtime_env_provider(parent_spec, plan) == {
        "nano-banana-pro": expected_connection,
        "seedance-2-prompt": expected_connection,
    }


@pytest.mark.asyncio
async def test_pipeline_hides_meta_skill_from_prompt_when_auto_trigger_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def noop_router(ctx: TurnContext) -> TurnContext:
        return ctx

    noop_router.__name__ = "apply_squilla_router"
    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", noop_router)

    loader = _make_loader_with_meta(tmp_path)
    runner = TurnRunner(provider_selector=None, config=_meta_cfg(auto_trigger=False))
    runner._skill_loader = loader

    turn, _provider = await runner._run_pipeline(
        "what is the capital of France?",  # non-triggering: isolates skills_filter
        "agent:main:test-meta-hidden",
        None,
        None,
        [
            ToolDefinition(name="web_search", description="search", input_schema=ToolInputSchema()),
        ],
        "base prompt",
        [],
    )

    assert "meta-tiny" not in str(turn.system_prompt)
    assert "meta-tiny" not in (turn.metadata.get("filtered_skill_ids") or [])


@pytest.mark.asyncio
async def test_pipeline_shows_meta_skill_when_auto_trigger_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def noop_router(ctx: TurnContext) -> TurnContext:
        return ctx

    noop_router.__name__ = "apply_squilla_router"
    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", noop_router)

    loader = _make_loader_with_meta(tmp_path)
    runner = TurnRunner(provider_selector=None, config=_meta_cfg(auto_trigger=True))
    runner._skill_loader = loader

    turn, _provider = await runner._run_pipeline(
        "what is the capital of France?",  # non-triggering: isolates skills_filter
        "agent:main:test-meta-shown",
        None,
        None,
        [
            ToolDefinition(
                name="meta_invoke",
                description="invoke",
                input_schema=ToolInputSchema(),
            ),
            ToolDefinition(name="web_search", description="search", input_schema=ToolInputSchema()),
        ],
        "base prompt",
        [],
    )

    assert "meta-tiny" in str(turn.system_prompt)


def test_build_tools_hides_meta_invoke_when_auto_trigger_off(tmp_path: Path) -> None:
    """Default manual-only: meta-skill present but auto_trigger off => no meta_invoke."""
    registry = get_default_registry()
    loader = _make_loader_with_meta(tmp_path)
    runner = TurnRunner(provider_selector=None, config=_meta_cfg(auto_trigger=False))
    runner._tool_registry = registry
    runner._skill_loader = loader

    ctx = ToolContext(is_owner=True, workspace_dir=str(tmp_path))
    tool_defs, _handler = runner._build_tools(ctx)
    names = {getattr(td, "name", "") for td in tool_defs}

    assert "meta_invoke" not in names
    assert ctx.surfaced_tools is None or "meta_invoke" not in ctx.surfaced_tools
