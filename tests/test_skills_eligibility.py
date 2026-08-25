"""Eligibility tests for skill manifest requirements."""

from __future__ import annotations

from types import SimpleNamespace

from opensquilla.skills.eligibility import EligibilityContext, diagnose_eligibility
from opensquilla.skills.types import (
    SkillInstallSpec,
    SkillLayer,
    SkillPlatformMeta,
    SkillRequires,
    SkillSpec,
)


def _env_any_skill() -> SkillSpec:
    return SkillSpec(
        name="env-any-skill",
        description="Synthetic skill with envAny requirements.",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="# body",
        metadata=SkillPlatformMeta(
            requires=SkillRequires(env_any=["OPENROUTER_API_KEY", "ARK_API_KEY"])
        ),
    )


def _git_skill() -> SkillSpec:
    return SkillSpec(
        name="git-skill",
        description="Synthetic skill that requires Git.",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="# body",
        metadata=SkillPlatformMeta(requires=SkillRequires(bins=["git"])),
    )


def test_env_any_is_satisfied_when_one_env_var_exists() -> None:
    report = diagnose_eligibility(
        _env_any_skill(),
        EligibilityContext(os_name="linux", env_cache={"ARK_API_KEY": "set"}),
    )

    assert report.eligible is True
    assert report.missing_env_any == []


def test_env_any_missing_when_no_alternative_exists() -> None:
    report = diagnose_eligibility(
        _env_any_skill(),
        EligibilityContext(
            os_name="linux",
            env_cache={"OPENROUTER_API_KEY": None, "ARK_API_KEY": None},
        ),
    )

    assert report.eligible is False
    assert report.missing_env_any == [["OPENROUTER_API_KEY", "ARK_API_KEY"]]
    assert (
        "Need one env var from: OPENROUTER_API_KEY, ARK_API_KEY" in report.reasons
    )


def test_install_metadata_counts_as_declared_dependencies() -> None:
    spec = SkillSpec(
        name="package-only-skill",
        description="Synthetic skill with package-only install metadata.",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="# body",
        metadata=SkillPlatformMeta(
            install=[
                SkillInstallSpec(
                    kind="uv",
                    id="pillow",
                    label="Install Pillow",
                    package="pillow",
                    module="PIL",
                )
            ]
        ),
    )

    report = diagnose_eligibility(spec, EligibilityContext(os_name="linux"))

    assert report.eligible is True
    assert report.declared is True


def test_git_requirement_uses_unified_capability_instead_of_path_lookup(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.git_runtime.resolve_git_capability",
        lambda: SimpleNamespace(available=False),
    )
    monkeypatch.setattr(
        "opensquilla.skills.eligibility.shutil.which",
        lambda _name: "/usr/bin/git",
    )

    report = diagnose_eligibility(_git_skill(), EligibilityContext(os_name="darwin"))

    assert report.eligible is False
    assert report.missing_bins == ["git"]
