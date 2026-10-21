"""Type definitions for the skills system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class SkillLayer(StrEnum):
    """Where a skill is loaded from (6-layer precedence, low→high)."""

    EXTRA = "extra"
    BUNDLED = "bundled"
    MANAGED = "managed"
    PERSONAL = "personal"
    PROJECT = "project"
    WORKSPACE = "workspace"


class SkillVisibility(StrEnum):
    """Which catalog surface may disclose a Skill."""

    PUBLIC = "public"
    INTERNAL = "internal"
    TOMBSTONE = "tombstone"
    EXPERIMENTAL = "experimental"


class SkillInvocation(StrEnum):
    """Which execution domain may load or invoke a Skill."""

    DIRECT = "direct"
    HISTORICAL_ONLY = "historical_only"
    EXPERIMENTAL_INTERNAL = "experimental_internal"


@dataclass
class SkillRequires:
    """Binary/env/config requirements for a skill."""

    bins: list[str] = field(default_factory=list)
    any_bins: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    env_any: list[str] = field(default_factory=list)
    config: list[str] = field(default_factory=list)


@dataclass
class SkillInstallSpec:
    """How to install a skill's dependencies."""

    kind: str = ""  # brew | node | go | uv | download | toolchain
    id: str = ""
    label: str = ""
    bins: list[str] = field(default_factory=list)
    os: list[str] = field(default_factory=list)
    formula: str = ""
    package: str = ""
    module: str = ""
    url: str = ""


@dataclass
class SkillPlatformMeta:
    """Platform requirements and metadata for a skill (OS, binaries, env, install)."""

    emoji: str = ""
    skill_key: str = ""
    primary_env: str = ""
    homepage: str = ""
    always: bool | None = None
    os: list[str] = field(default_factory=list)
    requires: SkillRequires | None = None
    install: list[SkillInstallSpec] = field(default_factory=list)


@dataclass(frozen=True)
class SkillProvenance:
    """Origin and stewardship metadata for release-facing skill surfaces."""

    origin: str = "unknown"
    license: str = "unknown"
    upstream_url: str = ""
    maintained_by: str = "OpenSquilla"


@dataclass
class SkillSpec:
    """Parsed skill metadata and content."""

    name: str
    description: str
    layer: SkillLayer
    always: bool
    triggers: list[str]
    content: str
    path: Path | None = None

    # Platform metadata
    metadata: SkillPlatformMeta | None = None
    provenance: SkillProvenance = field(default_factory=SkillProvenance)
    user_invocable: bool = True
    disable_model_invocation: bool = False
    # Optional localized (Simplified Chinese) one-line description. Falls back
    # to ``description`` (English) when absent. Sourced from the SKILL.md
    # front-matter ``description_zh`` field, mirroring the ``_zh/_en``
    # localization convention.
    description_zh: str = ""
    homepage: str = ""
    file_path: str = ""
    base_dir: str = ""
    # Conditional activation metadata
    requires_tools: list[str] = field(default_factory=list)
    fallback_for_toolsets: list[str] = field(default_factory=list)
    # Kept to reject unsupported historical manifests at catalog boundaries.
    kind: str = "skill"
    # Stable identity of this physical skill instance. Kept last so adding the
    # field does not shift any historical positional ``SkillSpec`` arguments.
    # Multiple layers may contribute the same logical ``name``; ``instance_id``
    # distinguishes the winning instance from shadowed candidates without
    # exposing host paths.
    instance_id: str = ""
    # Full content/type digest of the physical Skill tree at catalog compile
    # time. Supporting-resource reads compare this value with the live tree so
    # a turn pinned to an older catalog cannot combine old instructions with
    # files published by a newer install or reload.
    tree_digest: str = ""
    # Visibility and invocation constrain ordinary skill discovery and reads.
    visibility: SkillVisibility = SkillVisibility.PUBLIC
    invocation: SkillInvocation = SkillInvocation.DIRECT
