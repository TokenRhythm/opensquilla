"""Code-owned visibility and ordering for ordinary Skills."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from opensquilla.skills.types import (
    SkillInvocation,
    SkillLayer,
    SkillSpec,
    SkillVisibility,
)

# Deliberately tuples: their order is part of the prompt/cache contract.
PUBLIC_BUNDLED_SKILLS: tuple[str, ...] = (
    "deep-research",
    "docx",
    "github",
    "html-coder",
    "pdf-toolkit",
    "pptx",
    "skill-creator",
    "xlsx",
)

_LAYER_RANK: dict[SkillLayer, int] = {
    SkillLayer.BUNDLED: 0,
    SkillLayer.PERSONAL: 1,
    SkillLayer.MANAGED: 2,
    SkillLayer.PROJECT: 3,
    SkillLayer.WORKSPACE: 4,
    SkillLayer.EXTRA: 5,
}
_PUBLIC_BUNDLED_RANK = {name: index for index, name in enumerate(PUBLIC_BUNDLED_SKILLS)}


def packaged_bundled_root() -> Path:
    return Path(__file__).resolve().parent / "bundled"


def is_packaged_bundled_path(path: Path) -> bool:
    """Return whether ``path`` belongs to OpenSquilla's shipped catalog."""

    try:
        path.resolve().relative_to(packaged_bundled_root())
    except (OSError, ValueError):
        return False
    return True










def is_public_ordinary(skill: SkillSpec) -> bool:
    """Public ordinary projection shared by prompt, RPC, and model tools."""

    if bool(getattr(skill, "disable_model_invocation", False)):
        return False
    return _is_ordinary_domain(skill)


def is_user_invocable_ordinary(skill: SkillSpec) -> bool:
    """Public manual selection, including skills excluded from model discovery."""

    return bool(getattr(skill, "user_invocable", True)) and _is_ordinary_domain(
        skill,
    )




def public_sort_key(skill: SkillSpec) -> tuple[int, int, str, str]:
    """Stable prompt order: fixed bundled prefix, then stable layer/name order."""

    layer = getattr(skill, "layer", SkillLayer.EXTRA)
    name = str(getattr(skill, "name", ""))
    instance_id = str(getattr(skill, "instance_id", ""))
    if layer is SkillLayer.BUNDLED:
        rank = _PUBLIC_BUNDLED_RANK.get(name, len(_PUBLIC_BUNDLED_RANK))
        return (0, rank, name.casefold(), instance_id)
    return (
        1,
        _LAYER_RANK.get(layer, 99),
        name.casefold(),
        instance_id,
    )










def logical_locator(skill: SkillSpec, *, generation: int) -> str:
    """Return a stable, host-path-free locator for prompt/RPC metadata."""

    return f"skill://{skill.layer.value}/{skill.name}?generation={generation}"


def classify_packaged_bundled(skill: SkillSpec) -> SkillSpec:
    """Only explicitly supported shipped skills appear in the public catalog."""
    if skill.layer is SkillLayer.BUNDLED:
        skill.visibility = (
            SkillVisibility.PUBLIC if skill.name in PUBLIC_BUNDLED_SKILLS
            else SkillVisibility.INTERNAL
        )
        skill.invocation = (
            SkillInvocation.DIRECT if skill.name in PUBLIC_BUNDLED_SKILLS
            else SkillInvocation.EXPERIMENTAL_INTERNAL
        )
    return skill


def _is_ordinary_domain(skill: SkillSpec) -> bool:
    if (getattr(skill, "kind", "skill") != "skill"
            or getattr(skill, "name", "") == "code-task"):
        return False
    if getattr(skill, "visibility", SkillVisibility.PUBLIC) != SkillVisibility.PUBLIC:
        return False
    if getattr(skill, "invocation", SkillInvocation.DIRECT) != SkillInvocation.DIRECT:
        return False
    base_dir = str(getattr(skill, "base_dir", "") or "")
    if (getattr(skill, "layer", SkillLayer.EXTRA) is SkillLayer.BUNDLED
            and base_dir and is_packaged_bundled_path(Path(base_dir))):
        return skill.name in PUBLIC_BUNDLED_SKILLS
    return True


def project_public_catalog(skills: Iterable[SkillSpec]) -> list[SkillSpec]:
    return sorted((skill for skill in skills if is_public_ordinary(skill)), key=public_sort_key)


def can_view_skill(skill: SkillSpec, *, explicitly_selected: bool = False) -> bool:
    return is_public_ordinary(skill) or (
        explicitly_selected and is_user_invocable_ordinary(skill)
    )
