"""Tests for SkillLoader.list_meta_specs() helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.skills.loader import SkillLoader

BUNDLED = Path(__file__).resolve().parents[1].parent / "src" / "opensquilla" / "skills" / "bundled"


@pytest.fixture
def loader(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=snapshot)
    loader.invalidate_cache()
    loader.load_all()
    return loader


def test_list_meta_specs_returns_only_kind_meta(loader: SkillLoader) -> None:
    metas = loader.list_meta_specs()
    assert len(metas) > 0
    assert all(spec.kind == "meta" for spec in metas)


def test_list_meta_specs_includes_compiled_meta_sop(loader: SkillLoader) -> None:
    """meta-paper-write is authored as kind: meta_sop but loader Pass 2 compiles it to kind: meta.
    Post-compile it MUST appear in list_meta_specs — both shapes are valid pattern citations."""
    metas = loader.list_meta_specs()
    names = {s.name for s in metas}
    assert "meta-paper-write" in names


def test_list_meta_specs_includes_known_meta_bundles(loader: SkillLoader) -> None:
    names = {s.name for s in loader.list_meta_specs()}
    assert names == {
        "AwesomeWebpageMetaSkill",
        "meta-paper-write",
        "meta-short-drama",
        "meta-skill-creator",
    }


def test_retired_meta_skill_is_inspectable_but_not_discoverable(loader: SkillLoader) -> None:
    retired = loader.get_by_name("meta-kid-project-planner")

    assert retired is not None
    assert retired.kind == "meta"
    assert retired.user_invocable is False
    assert retired.disable_model_invocation is True
    assert retired.triggers == []
    assert retired.name not in {spec.name for spec in loader.list_meta_specs()}


def test_meta_short_drama_script_draft_requires_visible_script_text() -> None:
    skill_md = (BUNDLED / "meta-short-drama" / "SKILL.md").read_text(encoding="utf-8")

    assert "Do not call publish_artifact or any other tool." in skill_md
    assert "final" in skill_md
    assert "complete script itself" in skill_md
    assert "artifact marker" in skill_md
    assert '"[Used tool: ...]" placeholder' in skill_md
