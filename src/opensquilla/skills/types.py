"""Type definitions for the skills system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class SkillLayer(StrEnum):
    """Where a skill is loaded from (6-layer precedence, low→high)."""

    EXTRA = "extra"
    BUNDLED = "bundled"
    MANAGED = "managed"
    PERSONAL = "personal"
    PROJECT = "project"
    WORKSPACE = "workspace"


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

    kind: str = ""  # brew | node | go | uv | download
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
    # Risk metadata consumed by unattended meta-skill auto-enable. These are
    # advisory manifest fields, not runtime permissions.
    risk_level: str = ""
    capabilities: list[str] = field(default_factory=list)


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
    # convention already used by meta-skill clarify prompts.
    description_zh: str = ""
    homepage: str = ""
    file_path: str = ""
    base_dir: str = ""
    # Conditional activation metadata
    requires_tools: list[str] = field(default_factory=list)
    fallback_for_toolsets: list[str] = field(default_factory=list)
    # Meta-Skill metadata (MVP — non-meta skills keep defaults)
    kind: str = "skill"
    meta_priority: int = 0
    composition_raw: dict[str, object] | None = None
    # Optional. How MetaOrchestrator should derive ``final_text``:
    #   "auto" (default) — LLM post-processes step_outputs into a summary
    #   "raw"            — last non-substitute step output verbatim
    #   "step:<step_id>" — outputs[step_id] verbatim
    final_text_mode: str = "auto"
    # Optional meta-skill pre-flight request scaffold. Surfaces use this
    # to show the user what the meta-skill is about to do before the DAG
    # begins. Non-meta skills keep the default empty mapping.
    request_template: dict[str, Any] = field(default_factory=dict)
    # Optional final-answer contract surfaced after successful meta-skill
    # runs. The orchestrator renders a deterministic coverage block from
    # this metadata; future audit loops can consume the same shape.
    output_contract: dict[str, Any] = field(default_factory=dict)
    # Optional quality baseline prompts for meta-skill regression checks.
    # CI/cron can execute these prompts and judge outputs against the
    # manifest-owned rubric without inventing a separate eval catalog.
    eval_prompts: list[dict[str, Any]] = field(default_factory=list)
    # Optional stable preference keys a meta-skill may read/write across
    # sessions. Runtime memory integration remains separate; this manifest
    # field declares intent for authoring and policy review.
    preference_keys: list[str] = field(default_factory=list)
    # Optional policy tags for org-level governance and audit filters.
    policy_tags: list[str] = field(default_factory=list)
    # Wrapped-CLI manifest: when present, the skill can be invoked
    # deterministically by meta-skill ``skill_exec`` steps without spinning up
    # a sub-Agent. Schema (all keys optional except ``command``):
    #   command: str           — base executable + flags (Jinja-rendered)
    #   args: list[str]        — extra arguments (each Jinja-rendered)
    #   parse: "text" | "json" | "lines"  — how to interpret stdout (default text)
    #   timeout: float         — seconds before the subprocess is killed
    #   cwd: str               — working directory (defaults to base_dir)
    entrypoint: dict[str, Any] | None = None
