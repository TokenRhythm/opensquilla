"""Removed workflow assets cannot regain execution through older local state."""

from pathlib import Path

import pytest

from opensquilla.skills.catalog_policy import project_public_catalog
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.manifest import (
    RetiredSkillError,
    SkillCompileProfile,
    compile_skill_manifest,
    validate_hub_candidate,
)
from opensquilla.skills.types import SkillLayer


def _write(
    root: Path, name: str, kind: str = "skill", *, retired_fields: str = "",
) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Synthetic instructions\nkind: {kind}\n"
        f"{retired_fields}---\nSynthetic body.\n",
        encoding="utf-8",
    )
    return directory


@pytest.mark.parametrize("kind,name", [("meta", "old-flow"), ("meta_sop", "old-flow"),
                                       ("skill", "code-task")])
@pytest.mark.parametrize("profile", list(SkillCompileProfile))
def test_retired_manifests_are_rejected_in_all_compile_profiles(tmp_path, kind, name, profile):
    directory = _write(tmp_path, name, kind)
    original = (directory / "SKILL.md").read_bytes()
    with pytest.raises(RetiredSkillError, match="retired"):
        compile_skill_manifest(directory, SkillLayer.WORKSPACE, profile=profile)
    assert (directory / "SKILL.md").read_bytes() == original
    assert validate_hub_candidate(directory).ok is False


@pytest.mark.parametrize("retired_fields", [
    "visibility: internal\ninvocation: meta_only\n",
    "invocation: coding_only\n",
    "visibility: meta\n",
])
@pytest.mark.parametrize("profile", list(SkillCompileProfile))
def test_retired_execution_metadata_never_becomes_an_ordinary_skill(
    tmp_path, retired_fields, profile,
):
    directory = _write(tmp_path, "legacy-helper", retired_fields=retired_fields)
    original = (directory / "SKILL.md").read_bytes()

    with pytest.raises(RetiredSkillError, match="retired"):
        compile_skill_manifest(directory, SkillLayer.WORKSPACE, profile=profile)

    assert validate_hub_candidate(directory).ok is False
    assert (directory / "SKILL.md").read_bytes() == original


@pytest.mark.parametrize("retired_fields", [
    "visibility: internal\ninvocation: meta_only\n",
    "invocation: coding_only\n",
    "visibility: meta\n",
])
def test_retired_execution_metadata_does_not_degrade_catalog_reload(tmp_path, retired_fields):
    workspace = tmp_path / "skills"
    _write(workspace, "ordinary", retired_fields="visibility: public\ninvocation: direct\n")
    _write(workspace, "replaced")
    loader = SkillLoader(workspace_dir=workspace, snapshot_path=tmp_path / "cache.json")
    assert loader.get_by_name("replaced") is not None
    directory = _write(workspace, "replaced", retired_fields=retired_fields)
    original = (directory / "SKILL.md").read_bytes()

    result = loader.reload(reason="synthetic-retirement")

    assert result.success is True
    assert result.partial is False
    assert result.errors == ()
    assert loader.snapshot().errors == ()
    assert [skill.name for skill in project_public_catalog(loader.load_all())] == ["ordinary"]
    assert any("retired" in error.message and not error.kept_previous
               for error in loader.snapshot().diagnostics)
    assert (directory / "SKILL.md").read_bytes() == original

    restarted = SkillLoader(workspace_dir=workspace, snapshot_path=tmp_path / "cache.json")
    assert restarted.get_by_name("replaced") is None
    assert restarted.get_by_name("ordinary") is not None
    assert restarted.snapshot().errors == ()
    assert restarted.reload(force=True).changed is False


@pytest.mark.parametrize("profile", list(SkillCompileProfile))
def test_public_direct_definitions_remain_ordinary_skills(tmp_path, profile):
    directory = _write(
        tmp_path, "ordinary", retired_fields="visibility: public\ninvocation: direct\n",
    )

    skill = compile_skill_manifest(directory, SkillLayer.WORKSPACE, profile=profile)

    assert [item.name for item in project_public_catalog([skill])] == ["ordinary"]


@pytest.mark.parametrize("kind", ["meta", "meta_sop"])
def test_retirement_drops_previous_body_on_hot_reload(tmp_path, kind):
    workspace = tmp_path / "skills"
    _write(workspace, "ordinary")
    _write(workspace, "replaced")
    loader = SkillLoader(workspace_dir=workspace, snapshot_path=tmp_path / "cache.json")
    assert loader.get_by_name("replaced") is not None

    retired = _write(workspace, "replaced", kind)
    result = loader.reload(reason="synthetic-retirement")

    assert loader.get_by_name("replaced") is None
    assert [skill.name for skill in project_public_catalog(loader.load_all())] == ["ordinary"]
    assert result.success is True
    assert result.partial is False
    assert result.errors == ()
    assert loader.snapshot().errors == ()
    assert any("retired" in error.message and not error.kept_previous
               for error in loader.snapshot().diagnostics)
    assert (retired / "SKILL.md").exists()

    restarted = SkillLoader(workspace_dir=workspace, snapshot_path=tmp_path / "cache.json")
    assert restarted.get_by_name("replaced") is None
    assert restarted.get_by_name("ordinary") is not None
    assert restarted.snapshot().errors == ()
    assert restarted.reload(force=True).changed is False


def test_shipped_ordinary_catalog_has_no_removed_workflows(tmp_path):
    from opensquilla.skills.catalog_policy import PUBLIC_BUNDLED_SKILLS, packaged_bundled_root

    loader = SkillLoader(bundled_dir=packaged_bundled_root(), snapshot_path=tmp_path / "cache.json")
    catalog = project_public_catalog(loader.load_all())
    assert [skill.name for skill in catalog] == list(PUBLIC_BUNDLED_SKILLS)
    assert all(skill.kind == "skill" for skill in catalog)
    assert loader.get_by_name("code-task") is None
