from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import opensquilla.engine.steps.skills_filter as skills_filter_step
from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.steps.skills_filter import filter_skills
from opensquilla.gateway.config import GatewayConfig
from opensquilla.skills.eligibility import EligibilityContext
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.types import SkillLayer, SkillSpec

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "opensquilla" / "skills" / "bundled"
AUDIO_DEFAULTS = {
    "advanced-dubbing-studio",
    "music-and-singing-studio",
    "voice-clone-lab",
    "voice-conversion-studio",
    "voiceover-studio",
}
DEFAULTS = {
    "AwesomeWebpageMetaSkill",
    "ai-video-script",
    "audio-cog",
    "code-task",
    "cron",
    "deep-research",
    "docx",
    "filesystem",
    "git-diff",
    "github",
    "history-explorer",
    "html-coder",
    "html-to-pdf",
    "http-fetch",
    "latex-compile",
    "memory",
    "meta-paper-write",
    "meta-short-drama",
    "meta-skill-creator",
    "multi-search-engine",
    "nano-banana-pro",
    "nano-pdf",
    "paper-abstract-author",
    "paper-citation-planner",
    "paper-experiment-stub",
    "paper-outline-author",
    "paper-plot-stub",
    "paper-preference-planner",
    "paper-revision-author",
    "paper-source-curator",
    "pdf-toolkit",
    "pptx",
    "seedance-2-prompt",
    "skill-creator",
    "sub-agent",
    "subtitle-burner",
    "summarize",
    "text-file-read",
    "title-card-image",
    "tmux",
    "video-merger",
    "video-still-animator",
    "weather",
    "web-search",
    "xlsx",
} | AUDIO_DEFAULTS
PROMPT_DEFAULTS_WITHOUT_AUDIO_TOOLS = DEFAULTS - AUDIO_DEFAULTS
# Gated out of the default prompt unless coding mode is ON (default OFF).
CODING_MODE_GATED = {"code-task"}
INTERNAL_HELPERS = {
    "awesome-webpage-image-download",
    "awesome-webpage-research",
    "nano-banana-pro-openrouter",
    "openrouter-video-generator",
    # meta-DAG-internal pipeline fragments: reachable via a meta-skill's `skill:`
    # directive, never invoked standalone, so hidden from the model's index.
    "paper-artifact-runtime",
    "paper-citation-integrity-gate",
    "paper-delivery-summary",
    "paper-latex-sanitizer",
    "paper-length-gate",
    "paper-quality-gate",
    "paper-refbib-stub",
    "paper-section-author",
    "paper-source-readiness-gate",
    "skill-creator-linter",
    "skill-creator-proposals",
    "skill-creator-smoke-test",
    "short-drama-delivery-audit",
    "short-drama-review-normalizer",
    "srt-from-script",
    "stack-trace-generic-probe",
    "stack-trace-go-probe",
    "stack-trace-js-probe",
    "stack-trace-python-probe",
    "stack-trace-rust-probe",
}
COMPATIBILITY_TOMBSTONES = {"meta-kid-project-planner"}


def _ctx(
    loader: SkillLoader,
    tools: set[str] | None = None,
    *,
    message: str = "please summarize weather and github state",
    skills_config: SimpleNamespace | None = None,
) -> TurnContext:
    tool_defs = [
        SimpleNamespace(name=name)
        for name in (
            tools
            or {
                "background_process",
                "cron",
                "exec_command",
                "memory_get",
                "memory_save",
                "memory_search",
                "process",
            }
        )
    ]
    if skills_config is None:
        skills_config = SimpleNamespace(
            filter_enabled=False,
            max_skills_prompt_chars=100_000,
            injection_mode="system",
        )
    return TurnContext(
        message=message,
        session_key="agent:main:webchat:default",
        config=SimpleNamespace(
            tools=SimpleNamespace(profile="standard"),
            skills=skills_config,
        ),
        provider=None,
        model="test-model",
        tool_defs=tool_defs,
        system_prompt="base",
        metadata={"skill_loader": loader},
    )


def test_bundled_directory_only_contains_retained_skills_and_tombstones() -> None:
    bundled_names = {
        path.name
        for path in BUNDLED.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    }

    assert bundled_names == DEFAULTS | INTERNAL_HELPERS | COMPATIBILITY_TOMBSTONES


def test_skill_filter_defaults_are_release_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_SKILLS_FILTER_ENABLED", raising=False)
    monkeypatch.delenv("OPENSQUILLA_SKILLS_FILTER_STRATEGY", raising=False)

    cfg = GatewayConfig()

    assert cfg.skills.filter_enabled is False
    assert cfg.skills.filter_strategy == "lexical"


@pytest.mark.asyncio
async def test_default_prompt_only_injects_retained_bundled_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        skills_filter_step,
        "_elig_ctx",
        EligibilityContext(
            os_name="linux",
            has_bin_cache={
                "bibtex": True,
                "codex": True,
                "curl": True,
                "ffmpeg": True,
                "ffprobe": True,
                "gh": True,
                "xelatex": True,
                "nano-pdf": True,
                "python": True,
                "python3": True,
                "tmux": True,
            },
            env_cache={
                "ARK_API_KEY": "set",
                "BYTEPLUS_API_KEY": "set",
                "GEMINI_API_KEY": "set",
                "OPENAI_API_KEY": "set",
                "OPENROUTER_API_KEY": "set",
                "VOLC_ARK_API_KEY": "set",
            },
        ),
    )
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")

    ctx = _ctx(loader)
    # Meta-skills are injected only when meta auto-trigger is opted in
    # (manual-only is the default), so enable it here to assert the full
    # default catalog, including the kind="meta" entries.
    ctx.config.meta_skill = SimpleNamespace(enabled=True, auto_trigger=True)
    ctx = await filter_skills(ctx)

    prompt = ctx.system_prompt[1]
    for name in PROMPT_DEFAULTS_WITHOUT_AUDIO_TOOLS - CODING_MODE_GATED:
        assert f"<name>{name}</name>" in prompt
    for name in AUDIO_DEFAULTS:
        assert f"<name>{name}</name>" not in prompt
    for name in INTERNAL_HELPERS:
        assert f"<name>{name}</name>" not in prompt
    for name in COMPATIBILITY_TOMBSTONES:
        assert f"<name>{name}</name>" not in prompt
    # Coding-mode skills stay out of the default (coding mode OFF) prompt.
    for name in CODING_MODE_GATED:
        assert f"<name>{name}</name>" not in prompt
    assert "<name>healthcheck</name>" not in prompt


@pytest.mark.asyncio
async def test_coding_mode_on_surfaces_code_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # code-task requires the ``git`` bin (no env-var requirement: the subagent
    # inherits the operator's provider config); fake it so the test asserts
    # the coding-mode gate, not the host environment.
    monkeypatch.setattr(
        skills_filter_step,
        "_elig_ctx",
        EligibilityContext(
            os_name="linux",
            has_bin_cache={"git": True},
            env_cache={},
        ),
    )
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    ctx = _ctx(
        loader,
        message="please change the code",
        skills_config=SimpleNamespace(
            filter_enabled=False,
            max_skills_prompt_chars=100_000,
            injection_mode="system",
            coding_mode=True,
        ),
    )

    ctx = await filter_skills(ctx)

    prompt = ctx.system_prompt[1]
    assert "<name>code-task</name>" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_tool", ["background_process", "exec_command", "process"])
async def test_coding_mode_hides_code_task_without_required_launch_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_tool: str,
) -> None:
    monkeypatch.setattr(
        skills_filter_step,
        "_elig_ctx",
        EligibilityContext(
            os_name="linux",
            has_bin_cache={"git": True},
            env_cache={},
        ),
    )
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    tools = {"background_process", "exec_command", "process"}
    tools.remove(missing_tool)
    ctx = _ctx(
        loader,
        tools=tools,
        skills_config=SimpleNamespace(
            filter_enabled=False,
            max_skills_prompt_chars=100_000,
            injection_mode="system",
            coding_mode=True,
        ),
    )

    ctx = await filter_skills(ctx)

    assert "<name>code-task</name>" not in ctx.system_prompt[1]


@pytest.mark.asyncio
async def test_pinned_catalog_avoids_reloading_during_skill_injection() -> None:
    spec = SkillSpec(
        name="catalog-pinned",
        description="Pinned catalog skill",
        layer=SkillLayer.MANAGED,
        always=True,
        triggers=["pinned"],
        content="Use the pinned catalog.",
    )

    class _FailingLoader:
        def load_all(self):
            raise AssertionError("loader must not be read after the turn snapshot is pinned")

    ctx = _ctx(_FailingLoader())  # type: ignore[arg-type]
    ctx.skill_catalog = SimpleNamespace(skills=(spec,), generation=7)

    out = await filter_skills(ctx)

    assert "<name>catalog-pinned</name>" in out.system_prompt[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("injection_mode", ["system", "user_context", "user_message"])
async def test_production_skill_prompts_do_not_expose_host_locations(
    injection_mode: str,
) -> None:
    spec = SkillSpec(
        name="portable-community-skill",
        description="Portable community skill",
        layer=SkillLayer.MANAGED,
        always=True,
        triggers=[],
        content="Use this skill.",
        path=Path("/synthetic/host/skills/portable-community-skill/SKILL.md"),
        file_path="/synthetic/host/skills/portable-community-skill/SKILL.md",
    )

    class _FailingLoader:
        def load_all(self):
            raise AssertionError("loader must not be read after the turn snapshot is pinned")

    ctx = _ctx(
        _FailingLoader(),  # type: ignore[arg-type]
        skills_config=SimpleNamespace(
            filter_enabled=False,
            max_skills_prompt_chars=100_000,
            injection_mode=injection_mode,
        ),
    )
    ctx.skill_catalog = SimpleNamespace(skills=(spec,), generation=8)

    out = await filter_skills(ctx)
    if injection_mode == "user_context":
        prompt = out.metadata["skills_context_prompt"]
    else:
        prompt = out.system_prompt[1]

    assert "<name>portable-community-skill</name>" in prompt
    assert "<location>" not in prompt
    assert "/synthetic/host/skills" not in prompt


@pytest.mark.asyncio
async def test_default_prompt_prefers_matching_meta_skills_over_direct_answers(
    tmp_path: Path,
) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")

    ctx = _ctx(loader)
    # The meta-orchestration guidance is part of the prompt only when meta
    # auto-trigger is enabled; the manual-only default omits it.
    ctx.config.meta_skill = SimpleNamespace(enabled=True, auto_trigger=True)
    ctx = await filter_skills(ctx)

    prompt = ctx.system_prompt[1]
    assert "When a kind=\"meta\" entry clearly matches" in prompt
    assert "prefer `meta_invoke(name=\"<name>\")` over answering directly" in prompt
    assert "Do not call `skill_view` for kind=\"meta\" entries" in prompt
    assert "multi-skill orchestration" in prompt


@pytest.mark.asyncio
async def test_meta_skill_disabled_hides_meta_prompt_without_hiding_normal_skills(
    tmp_path: Path,
) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    ctx = _ctx(loader)
    ctx.config.meta_skill = SimpleNamespace(enabled=False)

    ctx = await filter_skills(ctx)
    prompt = ctx.system_prompt[1]

    assert "When a kind=\"meta\" entry clearly matches" not in prompt
    assert "prefer `meta_invoke(name=\"<name>\")` over answering directly" not in prompt
    assert "multi-skill orchestration" not in prompt
    assert "<available_skills>" in prompt


@pytest.mark.asyncio
async def test_internal_meta_helpers_stay_loadable_but_hidden_from_model(
    tmp_path: Path,
) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")

    ctx = await filter_skills(_ctx(loader))
    prompt = ctx.system_prompt[1]

    for name in INTERNAL_HELPERS:
        skill = loader.get_by_name(name)
        assert skill is not None
        assert skill.user_invocable is False
        assert skill.disable_model_invocation is True
        assert name not in {s.name for s in loader.get_user_invocable()}
        assert f"<name>{name}</name>" not in prompt


@pytest.mark.asyncio
async def test_allowlist_does_not_hide_managed_or_workspace_skills(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    skill_dir = managed / "custom-community"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: custom-community\n"
        "description: Use when testing managed skills.\n"
        "---\n\n# Custom\n",
        encoding="utf-8",
    )
    loader = SkillLoader(
        bundled_dir=BUNDLED,
        managed_dir=managed,
        snapshot_path=tmp_path / "snapshot.json",
    )

    ctx = await filter_skills(_ctx(loader))

    assert "<name>custom-community</name>" in ctx.system_prompt[1]


@pytest.mark.asyncio
async def test_lexical_skill_filter_is_opt_in_and_dependency_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    for name, description, triggers in (
        ("weather-local", "Fetch weather forecasts.", "[weather, forecast]"),
        ("github-local", "Inspect GitHub pull requests.", "[github, pull request]"),
    ):
        skill_dir = workspace / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"triggers: {triggers}\n"
            "---\n\n"
            f"# {name}\n",
            encoding="utf-8",
        )

    def fail_get_embedder(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("lexical skill filtering must not load embeddings")

    monkeypatch.setattr(skills_filter_step, "_retriever", None)
    monkeypatch.setattr("opensquilla.skills.retrieval.embedder.get_embedder", fail_get_embedder)
    loader = SkillLoader(workspace_dir=workspace, snapshot_path=tmp_path / "snapshot.json")

    ctx = await filter_skills(
        _ctx(
            loader,
            message="please check the weather forecast",
            skills_config=SimpleNamespace(
                filter_enabled=True,
                filter_top_k=1,
                filter_strategy="lexical",
                filter_lexical_top_n=20,
                filter_semantic_top_n=20,
                filter_rrf_k=60,
                filter_embedding_model="BAAI/bge-small-zh-v1.5",
                max_skills_prompt_chars=100_000,
                injection_mode="system",
            ),
        )
    )

    prompt = ctx.system_prompt[1]
    assert "<name>weather-local</name>" in prompt
    assert "<name>github-local</name>" not in prompt
    assert ctx.metadata["filtered_skill_ids"] == ["weather-local"]
    assert ctx.metadata["skill_count"] == 1


def test_workspace_overrides_managed_and_bundled_precedence(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    workspace = tmp_path / "workspace"
    for root, desc in ((managed, "managed desc"), (workspace, "workspace desc")):
        skill_dir = root / "github"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: github\ndescription: {desc}\n---\n\n# {desc}\n",
            encoding="utf-8",
        )

    loader = SkillLoader(
        bundled_dir=BUNDLED,
        managed_dir=managed,
        workspace_dir=workspace,
        snapshot_path=tmp_path / "snapshot.json",
    )

    skill = loader.get_by_name("github")

    assert skill is not None
    assert skill.description == "workspace desc"


@pytest.mark.asyncio
async def test_disable_model_invocation_hides_from_prompt_but_not_loader(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "hidden-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: hidden-skill\n"
        "description: Use when hidden testing.\n"
        "disable-model-invocation: true\n"
        "---\n\n# Hidden\n",
        encoding="utf-8",
    )
    loader = SkillLoader(
        bundled_dir=BUNDLED,
        workspace_dir=workspace,
        snapshot_path=tmp_path / "snapshot.json",
    )

    skill = loader.get_by_name("hidden-skill")
    ctx = await filter_skills(_ctx(loader))

    assert skill is not None
    assert skill.content == "# Hidden"
    assert "<name>hidden-skill</name>" not in ctx.system_prompt[1]


def test_retained_default_skills_are_parseable_and_not_disabled(tmp_path: Path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    skills = {skill.name: skill for skill in loader.load_all()}

    for name in DEFAULTS:
        assert name in skills
        assert skills[name].disable_model_invocation is False
        assert skills[name].description
