from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.steps.skill_catalog_projection import resolve_skill_catalog
from opensquilla.gateway import config_migration
from opensquilla.gateway.config import GatewayConfig, SkillsConfig
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_meta_runs import _handle_meta_inspect, _handle_meta_list
from opensquilla.gateway.rpc_skills import _handle_skills_list
from opensquilla.skills.catalog_policy import (
    PUBLIC_BUNDLED_SKILLS,
    STABLE_META_DEPENDENCIES,
    STABLE_META_SKILLS,
    project_public_catalog,
)
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.types import SkillLayer, SkillSpec
from opensquilla.tools.builtin import skill_tools as skill_tools_module
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import current_meta_skill_owner

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "opensquilla" / "skills" / "bundled"


def _loader(tmp_path: Path) -> SkillLoader:
    return SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")


def _ctx(loader: SkillLoader, *, coding_mode: bool = False, meta_auto: bool = True) -> TurnContext:
    config = GatewayConfig()
    config.skills.coding_mode = coding_mode
    config.skills.max_skills_prompt_chars = 100_000
    config.meta_skill.enabled = True
    config.meta_skill.auto_trigger = meta_auto
    snapshot = loader.snapshot_for_turn("test")
    return TurnContext(
        message="synthetic catalog contract",
        session_key="agent:main:test:catalog",
        config=config,
        provider=None,
        model="test-model",
        tool_defs=[
            SimpleNamespace(name="background_process"),
            SimpleNamespace(name="exec_command"),
            SimpleNamespace(name="process"),
        ],
        system_prompt=("base", "dynamic"),
        skill_catalog=snapshot,
    )


def _rendered_names(prompt: str) -> list[str]:
    names: list[str] = []
    for fragment in prompt.split("<name>")[1:]:
        names.append(fragment.split("</name>", 1)[0])
    return names


def test_public_bundled_contract_is_exact_and_ordered(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    snapshot = loader.snapshot_for_turn("test")
    projected = project_public_catalog(
        snapshot.skills,
        coding_mode=False,
        include_stable_meta=False,
    )
    bundled = [skill.name for skill in projected if skill.layer is SkillLayer.BUNDLED]
    assert bundled == list(PUBLIC_BUNDLED_SKILLS)


def test_stable_meta_roots_and_dependencies_remain_in_internal_snapshot(tmp_path: Path) -> None:
    snapshot = _loader(tmp_path).snapshot_for_turn("test")
    index = {skill.name: skill for skill in snapshot.skills}
    assert all(name in index for name in STABLE_META_SKILLS)
    for owner, dependencies in STABLE_META_DEPENDENCIES.items():
        assert index[owner].visibility == "meta"
        for dependency in dependencies:
            assert dependency in index
            assert index[dependency].visibility == "internal"
            assert owner in index[dependency].owner_meta_skills


@pytest.mark.asyncio
async def test_prompt_contains_public_eight_then_stable_meta_only(tmp_path: Path) -> None:
    output = await resolve_skill_catalog(_ctx(_loader(tmp_path)))
    base, suffix = output.system_prompt
    assert suffix == "dynamic"
    names = _rendered_names(base)
    assert names[: len(PUBLIC_BUNDLED_SKILLS)] == list(PUBLIC_BUNDLED_SKILLS)
    assert names[len(PUBLIC_BUNDLED_SKILLS) :] == [
        name for name in STABLE_META_SKILLS if name in names
    ]
    assert "meta-skill-creator" in names
    assert "paper-section-author" not in base
    assert "meta-kid-project-planner" not in base
    assert output.metadata["skills_catalog_omitted_count"] == 0


@pytest.mark.asyncio
async def test_manual_meta_mode_removes_meta_roots_from_prompt(tmp_path: Path) -> None:
    output = await resolve_skill_catalog(_ctx(_loader(tmp_path), meta_auto=False))
    assert _rendered_names(output.system_prompt[0]) == list(PUBLIC_BUNDLED_SKILLS)


@pytest.mark.asyncio
async def test_code_task_is_visible_only_in_coding_mode(tmp_path: Path) -> None:
    off = await resolve_skill_catalog(_ctx(_loader(tmp_path), coding_mode=False))
    on = await resolve_skill_catalog(_ctx(_loader(tmp_path), coding_mode=True))
    assert "code-task" not in _rendered_names(off.system_prompt[0])
    assert "code-task" in _rendered_names(on.system_prompt[0])


@pytest.mark.asyncio
async def test_projection_is_message_independent_within_generation(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    first = _ctx(loader)
    second = _ctx(loader)
    second.message = "completely different synthetic request"
    one = await resolve_skill_catalog(first)
    two = await resolve_skill_catalog(second)
    assert one.system_prompt == two.system_prompt
    assert one.metadata["skill_catalog_ids"] == two.metadata["skill_catalog_ids"]


def test_removed_filter_configuration_is_not_in_schema() -> None:
    fields = set(SkillsConfig.model_fields)
    assert not fields.intersection(
        {
            "filter_enabled",
            "filter_top_k",
            "filter_strategy",
            "filter_lexical_top_n",
            "filter_semantic_top_n",
            "filter_rrf_k",
            "filter_embedding_model",
        }
    )


def test_legacy_filter_toml_is_cleaned_before_validation_and_rewritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[skills]\n"
        "filter_enabled = true\n"
        "filter_top_k = 3\n"
        'filter_strategy = "hybrid"\n'
        "filter_lexical_top_n = 7\n"
        "filter_semantic_top_n = 9\n"
        "filter_rrf_k = 33\n"
        'filter_embedding_model = "legacy-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_migration, "_LEGACY_SKILL_FILTER_WARNED", False)
    with pytest.warns(DeprecationWarning, match="relevance-filter"):
        GatewayConfig.load(config_path)
    rewritten = config_path.read_text(encoding="utf-8")
    assert "filter_" not in rewritten
    assert list(tmp_path.glob("config.toml.backup.*"))


def test_legacy_filter_environment_is_ignored_and_warned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_migration, "_LEGACY_SKILL_FILTER_WARNED", False)
    monkeypatch.setenv("OPENSQUILLA_SKILLS_FILTER_ENABLED", "true")
    with pytest.warns(DeprecationWarning, match="relevance-filter"):
        cfg = GatewayConfig()
    assert "filter_enabled" not in type(cfg.skills).model_fields


def test_user_owned_layers_remain_public_and_stably_sorted() -> None:
    def spec(name: str, layer: SkillLayer) -> SkillSpec:
        return SkillSpec(name, name, layer, False, [], "", instance_id=f"{layer}:{name}")

    projected = project_public_catalog(
        [
            spec("zeta", SkillLayer.WORKSPACE),
            spec("beta", SkillLayer.PERSONAL),
            spec("alpha", SkillLayer.MANAGED),
            spec("gamma", SkillLayer.PROJECT),
        ],
        coding_mode=False,
        include_stable_meta=False,
    )
    assert [(skill.layer, skill.name) for skill in projected] == [
        (SkillLayer.PERSONAL, "beta"),
        (SkillLayer.MANAGED, "alpha"),
        (SkillLayer.PROJECT, "gamma"),
        (SkillLayer.WORKSPACE, "zeta"),
    ]


@pytest.mark.asyncio
async def test_rpc_separates_ordinary_and_meta_catalogs(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    ctx = RpcContext(conn_id="test", skill_loader=loader)
    ordinary = await _handle_skills_list(None, ctx)
    metas = await _handle_meta_list(None, ctx)
    assert [row["name"] for row in ordinary["skills"]] == list(PUBLIC_BUNDLED_SKILLS)
    assert [row["name"] for row in metas["skills"]] == list(STABLE_META_SKILLS)

    detail = await _handle_meta_inspect({"name": "meta-paper-write"}, ctx)
    assert [item["name"] for item in detail["dependencies"]] == list(
        STABLE_META_DEPENDENCIES["meta-paper-write"]
    )
    assert all(item["visibility"] == "internal" for item in detail["dependencies"])
    assert "content" not in detail


@pytest.mark.asyncio
async def test_internal_body_requires_trusted_meta_execution_domain(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    previous = skill_tools_module._loader
    skill_tools_module.create_skill_tools(loader)
    registered = get_default_registry().get("skill_view")
    assert registered is not None
    try:
        denied = await registered.handler(name="paper-section-author", file_path=None)
        assert "Skill not found" in denied

        wrong = current_meta_skill_owner.set("meta-short-drama")
        try:
            denied_wrong_owner = await registered.handler(
                name="paper-section-author",
                file_path=None,
            )
        finally:
            current_meta_skill_owner.reset(wrong)
        assert "Skill not found" in denied_wrong_owner

        trusted = current_meta_skill_owner.set("meta-paper-write")
        try:
            body = await registered.handler(name="paper-section-author", file_path=None)
        finally:
            current_meta_skill_owner.reset(trusted)
        assert "You are drafting one section" in body

        meta_root = await registered.handler(name="meta-paper-write", file_path=None)
        assert "Skill not found" in meta_root
    finally:
        skill_tools_module._loader = previous
