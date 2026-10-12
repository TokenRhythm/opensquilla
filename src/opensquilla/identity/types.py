"""Identity data types for agent persona and profile."""

from dataclasses import dataclass, field


@dataclass
class IdentityFields:
    """Parsed fields from IDENTITY.md."""

    name: str | None = None
    emoji: str | None = None
    creature: str | None = None
    vibe: str | None = None
    theme: str | None = None
    avatar: str | None = None


@dataclass
class SoulDocument:
    """Parsed SOUL.md: optional YAML frontmatter + markdown body."""

    body: str = ""
    frontmatter: dict[str, str] = field(default_factory=dict)


@dataclass
class AgentCapability:
    """A single declared capability from AGENTS.md."""

    name: str
    description: str = ""


@dataclass
class AgentsDocument:
    """Parsed AGENTS.md: project rules and agent behavioral directives."""

    raw: str = ""
    capabilities: list[AgentCapability] = field(default_factory=list)


@dataclass
class AgentIdentity:
    """Resolved identity for an agent — merges config + file sources."""

    name: str | None = None
    emoji: str | None = None
    theme: str | None = None
    avatar: str | None = None
    # Source documents (may be None if not present)
    soul: SoulDocument | None = None
    identity_fields: IdentityFields | None = None


@dataclass
class AgentProfile:
    """Complete agent profile combining identity and workspace context."""

    agent_id: str
    identity: AgentIdentity = field(default_factory=AgentIdentity)
    agents_doc: AgentsDocument | None = None
    workspace_files: dict[str, str] = field(default_factory=dict)
    # Prompt assembly mode: "full" | "minimal" | "none" |
    # "headless_source_edit" | "headless_repo_coding_scaffold"
    prompt_mode: str = "full"
    # Opt-in additive "Patch Evidence Protocol" prompt section for
    # repo-coding/patching sessions (see engine.runtime
    # _resolve_patch_evidence_protocol). Composes with any prompt_mode.
    patch_evidence_protocol: bool = False
    # Opt-in additive "Reproduction Evidence" prompt section paired with the
    # loop-side finalize-time red-evidence gate (see engine.runtime
    # _resolve_finalize_evidence_gate). Composes with any prompt_mode.
    finalize_evidence_gate: bool = False
    # Opt-in switch restoring the earlier compact "Tool Call Style" and
    # "Reply Guidelines" prompt directives (see engine.runtime
    # _resolve_legacy_prompt_style). Off keeps the current wording unchanged.
    legacy_prompt_style: bool = False
    # Per-turn time-prefix on user messages (see engine.steps.inject_time_prefix).
    inject_time_prefix: bool = True
