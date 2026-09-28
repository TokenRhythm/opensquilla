from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.steps.skill_catalog_projection import resolve_skill_catalog
from opensquilla.gateway import config_migration
from opensquilla.gateway.config import GatewayConfig, SkillsConfig
from opensquilla.skills.catalog_policy import (
    PUBLIC_BUNDLED_SKILLS,
    project_public_catalog,
)
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.types import SkillLayer, SkillSpec

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "opensquilla" / "skills" / "bundled"


def _loader(tmp_path: Path) -> SkillLoader:
    return SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")


def _ctx(loader: SkillLoader) -> TurnContext:
    config = GatewayConfig()
    config.skills.max_skills_prompt_chars = 100_000
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
    )
    bundled = [skill.name for skill in projected if skill.layer is SkillLayer.BUNDLED]
    assert bundled == list(PUBLIC_BUNDLED_SKILLS)




@pytest.mark.asyncio
async def test_prompt_contains_only_public_ordinary_skills(tmp_path: Path) -> None:
    output = await resolve_skill_catalog(_ctx(_loader(tmp_path)))
    base, suffix = output.system_prompt
    assert suffix == "dynamic"
    names = _rendered_names(base)
    assert names[: len(PUBLIC_BUNDLED_SKILLS)] == list(PUBLIC_BUNDLED_SKILLS)
    assert names == list(PUBLIC_BUNDLED_SKILLS)
    assert output.metadata["skills_catalog_omitted_count"] == 0






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


@pytest.mark.parametrize("leaf", sorted(config_migration.DEPRECATED_SKILL_FILTER_LEAVES))
@pytest.mark.parametrize("prefix", ["OPENSQUILLA_SKILLS_", "OPENSQUILLA_GATEWAY_SKILLS__"])
@pytest.mark.parametrize("from_file", [False, True], ids=["constructor", "config-file"])
def test_retired_filter_environment_does_not_block_gateway_start(
    leaf: str,
    prefix: str,
    from_file: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_migration, "_LEGACY_SKILL_FILTER_WARNED", False)
    monkeypatch.setenv("OPENSQUILLA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(f"{prefix}{leaf.upper()}", "retired-value-do-not-parse")
    path = tmp_path / "config.toml"
    config_text = (
        f"config_version = {config_migration.LATEST_CONFIG_VERSION}\n"
        "[skills]\nmax_skills_prompt_chars = 1234\n"
    )
    path.write_text(config_text, encoding="utf-8")

    with pytest.warns(DeprecationWarning, match="relevance-filter") as warnings:
        config = (
            GatewayConfig.load(path)
            if from_file
            else GatewayConfig(skills={"max_skills_prompt_chars": 1234})
        )
    assert len(warnings) == 1
    assert config.skills.max_skills_prompt_chars == 1234
    assert not set(config.skills.model_dump()).intersection(
        config_migration.DEPRECATED_SKILL_FILTER_LEAVES
    )
    assert path.read_text(encoding="utf-8") == config_text


@pytest.mark.parametrize("source", ["constructor", "config-file", "nested-env"])
def test_retired_filter_cleanup_still_rejects_unknown_settings(
    source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config_migration, "_LEGACY_SKILL_FILTER_WARNED", True)
    payload = {"filter_enabled": "unused", "filter_stratgey": "hybrid"}
    with pytest.raises(ValidationError, match="filter_stratgey"):
        if source == "constructor":
            GatewayConfig(skills=payload)
        elif source == "config-file":
            path = tmp_path / "config.toml"
            path.write_text(
                '[skills]\nfilter_enabled = "unused"\nfilter_stratgey = "hybrid"\n',
                encoding="utf-8",
            )
            GatewayConfig.load(path)
        else:
            monkeypatch.setenv("OPENSQUILLA_GATEWAY_SKILLS__FILTER_ENABLED", "unused")
            monkeypatch.setenv("OPENSQUILLA_GATEWAY_SKILLS__FILTER_STRATGEY", "hybrid")
            GatewayConfig()
    assert payload == {"filter_enabled": "unused", "filter_stratgey": "hybrid"}


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
    )
    assert [(skill.layer, skill.name) for skill in projected] == [
        (SkillLayer.PERSONAL, "beta"),
        (SkillLayer.MANAGED, "alpha"),
        (SkillLayer.PROJECT, "gamma"),
        (SkillLayer.WORKSPACE, "zeta"),
    ]
