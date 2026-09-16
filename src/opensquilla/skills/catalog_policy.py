"""Code-owned Skill catalog visibility and ordering policy.

The loader retains every compiled candidate needed by Meta workflows,
recovery, and lifecycle operations.  Public prompt/RPC/tool surfaces consume
the projections in this module instead of inferring visibility from whatever
happens to exist on disk.
"""

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

STABLE_META_SKILLS: tuple[str, ...] = (
    "meta-paper-write",
    "meta-short-drama",
    "meta-skill-creator",
)

STABLE_META_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "meta-paper-write": (
        "multi-search-engine",
        "paper-refbib-stub",
        "paper-source-readiness-gate",
        "paper-section-author",
        "paper-artifact-runtime",
        "paper-latex-sanitizer",
        "paper-length-gate",
        "paper-citation-integrity-gate",
        "paper-quality-gate",
        "paper-delivery-summary",
    ),
    "meta-short-drama": (
        "ai-video-script",
        "short-drama-review-normalizer",
        "nano-banana-pro",
        "seedance-2-prompt",
        "short-drama-delivery-audit",
        "video-still-animator",
        "video-merger",
        "srt-from-script",
        "subtitle-burner",
        "title-card-image",
        "text-file-read",
    ),
    "meta-skill-creator": ("history-explorer",),
}

COMPATIBILITY_TOMBSTONES: frozenset[str] = frozenset({"meta-kid-project-planner"})
EXPERIMENTAL_META_SKILLS: frozenset[str] = frozenset({"AwesomeWebpageMetaSkill"})
EXPERIMENTAL_META_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "AwesomeWebpageMetaSkill": (
        "awesome-webpage-research",
        "web-search",
        "awesome-webpage-image-download",
        "nano-banana-pro-openrouter",
        "audio-cog",
        "openrouter-video-generator",
        "filesystem",
    )
}
CODING_MODE_SKILLS: frozenset[str] = frozenset({"code-task"})

_DEPENDENCY_OWNERS: dict[str, tuple[str, ...]] = {}
for _owner, _dependencies in STABLE_META_DEPENDENCIES.items():
    for _dependency in _dependencies:
        _DEPENDENCY_OWNERS[_dependency] = (*_DEPENDENCY_OWNERS.get(_dependency, ()), _owner)
for _owner, _dependencies in EXPERIMENTAL_META_DEPENDENCIES.items():
    for _dependency in _dependencies:
        _DEPENDENCY_OWNERS[_dependency] = (*_DEPENDENCY_OWNERS.get(_dependency, ()), _owner)

_LAYER_RANK: dict[SkillLayer, int] = {
    SkillLayer.BUNDLED: 0,
    SkillLayer.PERSONAL: 1,
    SkillLayer.MANAGED: 2,
    SkillLayer.PROJECT: 3,
    SkillLayer.WORKSPACE: 4,
    SkillLayer.EXTRA: 5,
}
_PUBLIC_BUNDLED_RANK = {name: index for index, name in enumerate(PUBLIC_BUNDLED_SKILLS)}
_META_RANK = {name: index for index, name in enumerate(STABLE_META_SKILLS)}


def packaged_bundled_root() -> Path:
    return Path(__file__).resolve().parent / "bundled"


def is_packaged_bundled_path(path: Path) -> bool:
    """Return whether ``path`` belongs to OpenSquilla's shipped catalog."""

    try:
        path.resolve().relative_to(packaged_bundled_root())
    except (OSError, ValueError):
        return False
    return True


def classify_packaged_bundled(skill: SkillSpec) -> SkillSpec:
    """Apply the fail-closed policy to one shipped bundled manifest."""

    if skill.layer is not SkillLayer.BUNDLED:
        return skill
    name = skill.name
    if name in PUBLIC_BUNDLED_SKILLS:
        skill.visibility = SkillVisibility.PUBLIC
        skill.invocation = SkillInvocation.DIRECT
    elif name in STABLE_META_SKILLS:
        skill.visibility = SkillVisibility.META
        skill.invocation = SkillInvocation.META_ONLY
    elif name in _DEPENDENCY_OWNERS:
        skill.visibility = SkillVisibility.INTERNAL
        skill.invocation = SkillInvocation.META_ONLY
        skill.owner_meta_skills = list(_DEPENDENCY_OWNERS[name])
    elif name in CODING_MODE_SKILLS:
        skill.visibility = SkillVisibility.PUBLIC
        skill.invocation = SkillInvocation.CODING_ONLY
    elif name in COMPATIBILITY_TOMBSTONES:
        skill.visibility = SkillVisibility.TOMBSTONE
        skill.invocation = SkillInvocation.HISTORICAL_ONLY
    elif name in EXPERIMENTAL_META_SKILLS or skill.kind == "meta":
        skill.visibility = SkillVisibility.EXPERIMENTAL
        skill.invocation = SkillInvocation.EXPERIMENTAL_INTERNAL
    else:
        # Shipped helpers and legacy public entries must be opted into one of
        # the explicit sets above; mere directory presence never publishes one.
        skill.visibility = SkillVisibility.INTERNAL
        skill.invocation = SkillInvocation.EXPERIMENTAL_INTERNAL
    return skill


def is_stable_meta_root(skill: SkillSpec) -> bool:
    return (
        skill.layer is SkillLayer.BUNDLED
        and skill.name in STABLE_META_SKILLS
        and skill.visibility is SkillVisibility.META
    )


def is_invokable_meta(skill: SkillSpec) -> bool:
    """Return whether a definition belongs to the supported Meta domain.

    Availability is deliberately checked by the caller.  Keeping the domain
    check separate lets fresh launches produce a precise disabled/retired
    diagnostic while still excluding experimental shipped Meta definitions.
    ``getattr`` defaults preserve compatibility with embedders that construct
    lightweight Meta spec objects instead of going through the manifest
    compiler; compiled specs always carry both policy fields.
    """

    if getattr(skill, "kind", "skill") != "meta":
        return False
    return getattr(skill, "visibility", SkillVisibility.META) in {
        SkillVisibility.META,
        "meta",
    } and getattr(skill, "invocation", SkillInvocation.META_ONLY) in {
        SkillInvocation.META_ONLY,
        "meta_only",
    }


def owners_for_meta_dependency(name: str) -> tuple[str, ...]:
    return _DEPENDENCY_OWNERS.get(name, ())


def is_public_ordinary(skill: SkillSpec, *, coding_mode: bool) -> bool:
    """Public ordinary projection shared by prompt, RPC, and model tools."""

    if bool(getattr(skill, "disable_model_invocation", False)):
        return False
    name = str(getattr(skill, "name", ""))
    layer = getattr(skill, "layer", SkillLayer.EXTRA)
    if name in CODING_MODE_SKILLS:
        return coding_mode and getattr(skill, "visibility", SkillVisibility.PUBLIC) in {
            SkillVisibility.PUBLIC,
            "public",
        }
    base_dir = str(getattr(skill, "base_dir", "") or "")
    shipped = bool(base_dir and is_packaged_bundled_path(Path(base_dir)))
    if layer is SkillLayer.BUNDLED and shipped:
        if skill.name in PUBLIC_BUNDLED_SKILLS:
            return skill.visibility is SkillVisibility.PUBLIC
        return False
    # Personal/Managed/Project/Workspace/Extra retain historical extensibility.
    return (
        getattr(skill, "kind", "skill") != "meta"
        and getattr(skill, "visibility", SkillVisibility.PUBLIC)
        in {SkillVisibility.PUBLIC, "public"}
        and getattr(skill, "invocation", SkillInvocation.DIRECT)
        in {SkillInvocation.DIRECT, "direct"}
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


def meta_sort_key(skill: SkillSpec) -> tuple[int, int, str, str]:
    if skill.name in _META_RANK and skill.layer is SkillLayer.BUNDLED:
        return (0, _META_RANK[skill.name], skill.name, skill.instance_id)
    return (
        1,
        _LAYER_RANK.get(skill.layer, 99),
        skill.name.casefold(),
        skill.instance_id,
    )


def project_public_catalog(
    skills: Iterable[SkillSpec],
    *,
    coding_mode: bool,
    include_stable_meta: bool,
) -> list[SkillSpec]:
    ordinary = sorted(
        (skill for skill in skills if is_public_ordinary(skill, coding_mode=coding_mode)),
        key=public_sort_key,
    )
    if not include_stable_meta:
        return ordinary
    metas = sorted((skill for skill in skills if is_invokable_meta(skill)), key=meta_sort_key)
    return [*ordinary, *metas]


def project_creator_catalog(skills: Iterable[SkillSpec]) -> list[SkillSpec]:
    """Capabilities a stable MetaSkill creator may compose into a proposal.

    Candidate DAGs may use ordinary public Skills plus the creator's own
    deterministic history dependency. Other Meta internals stay outside this
    execution domain even though they remain in the loader snapshot.
    """

    projected = project_public_catalog(
        skills,
        coding_mode=False,
        include_stable_meta=False,
    )
    own_dependencies = [
        skill
        for skill in skills
        if skill.name in STABLE_META_DEPENDENCIES["meta-skill-creator"]
        and "meta-skill-creator" in skill.owner_meta_skills
    ]
    return [*projected, *sorted(own_dependencies, key=public_sort_key)]


def can_view_skill(
    skill: SkillSpec,
    *,
    coding_mode: bool,
    owner_meta_skill: str = "",
) -> bool:
    """Authorize a lazy body read without widening the public catalog."""

    if is_public_ordinary(skill, coding_mode=coding_mode):
        return True
    return bool(
        owner_meta_skill
        and skill.invocation is SkillInvocation.META_ONLY
        and owner_meta_skill in skill.owner_meta_skills
    )


def logical_locator(skill: SkillSpec, *, generation: int) -> str:
    """Return a stable, host-path-free locator for prompt/RPC metadata."""

    return f"skill://{skill.layer.value}/{skill.name}?generation={generation}"
