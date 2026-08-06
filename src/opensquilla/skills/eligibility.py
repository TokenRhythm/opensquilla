"""Eligibility filtering — checks if a skill is usable in the current environment."""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, field

from opensquilla.skills.toolchains import (
    resolve_managed_binary,
    resolve_managed_binary_passive,
)
from opensquilla.skills.types import SkillInstallSpec, SkillSpec


@dataclass
class EligibilityContext:
    """Environment context for eligibility checks."""

    os_name: str = ""
    has_bin_cache: dict[str, bool] = field(default_factory=dict)
    env_cache: dict[str, str | None] = field(default_factory=dict)
    enabled_set: set[str] | None = None  # None = all enabled
    disabled_set: set[str] = field(default_factory=set)
    passive_managed_bins: bool = False

    @staticmethod
    def auto(
        enabled_set: set[str] | None = None,
        disabled_set: set[str] | None = None,
    ) -> EligibilityContext:
        """Build context from the current environment."""
        return EligibilityContext(
            os_name=platform.system().lower(),
            enabled_set=enabled_set,
            disabled_set=disabled_set or set(),
        )


def _has_bin(name: str, ctx: EligibilityContext) -> bool:
    if name in ctx.has_bin_cache:
        return ctx.has_bin_cache[name]
    # Binary requirements are names, never manifest-controlled filesystem paths.
    # resolve_managed_binary consults only code-catalogued, validated managed
    # activation receipts in addition to the legacy system lookup below.
    safe_name = bool(
        name
        and name not in {".", ".."}
        and "\x00" not in name
        and "/" not in name
        and "\\" not in name
    )
    result = False
    if safe_name:
        # Keep the long-standing system lookup seam (and system PATH priority)
        # before consulting validated OpenSquilla activation receipts.
        result = shutil.which(name) is not None
        if not result:
            try:
                resolver = (
                    resolve_managed_binary_passive
                    if ctx.passive_managed_bins
                    else resolve_managed_binary
                )
                result = resolver(name) is not None
            except (OSError, TypeError, ValueError):
                # Managed state is an optional enhancement. Corrupt/unreadable
                # receipts must fail closed without breaking the whole catalog.
                result = False
    ctx.has_bin_cache[name] = result
    return result


def _has_env(name: str, ctx: EligibilityContext) -> bool:
    if name in ctx.env_cache:
        cached = ctx.env_cache[name]
        return isinstance(cached, str) and bool(cached.strip())
    val = os.environ.get(name)
    ctx.env_cache[name] = val
    return isinstance(val, str) and bool(val.strip())


def check_eligibility(spec: SkillSpec, ctx: EligibilityContext) -> bool:
    """Check if a skill is eligible in the current environment.

    Returns False if any hard requirement is not met.
    """
    # 1. Explicitly disabled
    if spec.name in ctx.disabled_set:
        return False

    # 2. Explicitly enabled (whitelist mode)
    if ctx.enabled_set is not None and spec.name not in ctx.enabled_set:
        return False

    meta = spec.metadata
    if meta is None:
        return True  # No requirements → always eligible

    # 3. OS check
    if meta.os and ctx.os_name and ctx.os_name not in meta.os:
        return False

    # 4. Required bins (all must exist)
    if meta.requires:
        for b in meta.requires.bins:
            if not _has_bin(b, ctx):
                return False

        # 5. anyBins (at least one must exist)
        if meta.requires.any_bins:
            if not any(_has_bin(b, ctx) for b in meta.requires.any_bins):
                return False

        # 6. Required env vars
        for e in meta.requires.env:
            if not _has_env(e, ctx):
                return False

        # 7. envAny (at least one env var must exist)
        if meta.requires.env_any:
            if not any(_has_env(e, ctx) for e in meta.requires.env_any):
                return False

    return True


# ---------------------------------------------------------------------------
# Diagnostic report — detailed "why ineligible" + install hints
# ---------------------------------------------------------------------------


@dataclass
class InstallHint:
    """Display-only install command, decoupled from dependency execution logic."""

    kind: str  # "brew", "uv", "npm", "go", "download", "toolchain"
    label: str  # "Install himalaya (brew)"
    command: str  # "brew install himalaya"


@dataclass
class EligibilityReport:
    """Structured diagnosis of why a skill is or isn't eligible."""

    eligible: bool
    reasons: list[str] = field(default_factory=list)
    missing_bins: list[str] = field(default_factory=list)
    missing_env: list[str] = field(default_factory=list)
    missing_env_any: list[list[str]] = field(default_factory=list)
    install_hints: list[InstallHint] = field(default_factory=list)
    disabled: bool = False
    wrong_os: bool = False
    declared: bool = False


def _is_declared(spec: SkillSpec) -> bool:
    """Return True when the skill's frontmatter declares runtime requirements.

    Frontmatter with only ``metadata.emoji`` and no ``requires.*`` is not a
    declaration. ``requires.config`` is excluded — reserved/future,
    doesn't currently affect eligibility.
    """
    if spec.metadata is None:
        return False
    requires = spec.metadata.requires
    requires_declared = bool(
        requires and (requires.bins or requires.any_bins or requires.env or requires.env_any)
    )
    return requires_declared or bool(spec.metadata.install)


def _render_install_command(spec: SkillInstallSpec) -> str:
    """Render a display-only shell command from an install spec."""
    name = spec.formula or spec.package or spec.id
    if spec.kind == "brew":
        return f"brew install {name}" if name else ""
    if spec.kind == "uv":
        return f"uv pip install {spec.package}" if spec.package else ""
    if spec.kind == "npm":
        return f"npm install -g {spec.package}" if spec.package else ""
    if spec.kind == "go":
        return f"go install {spec.module}@latest" if spec.module else ""
    if spec.kind == "download" and spec.url:
        bin_name = spec.bins[0] if spec.bins else spec.id
        return (
            f"curl -fsSL -o ~/.local/bin/{bin_name} {spec.url} && chmod +x ~/.local/bin/{bin_name}"
        )
    return ""


def diagnose_eligibility(spec: SkillSpec, ctx: EligibilityContext) -> EligibilityReport:
    """Detailed diagnosis: calls check_eligibility for the gate, then collects reasons.

    The boolean in the report is always authoritative (from check_eligibility).
    The detail fields explain *why* the skill is ineligible.
    """
    eligible = check_eligibility(spec, ctx)
    if eligible:
        return EligibilityReport(eligible=True, declared=_is_declared(spec))

    reasons: list[str] = []
    missing_bins: list[str] = []
    missing_env: list[str] = []
    missing_env_any: list[list[str]] = []
    disabled = False
    wrong_os = False

    # Walk each check category to collect detail
    if spec.name in ctx.disabled_set:
        disabled = True
        reasons.append(f"Skill '{spec.name}' is disabled")

    if ctx.enabled_set is not None and spec.name not in ctx.enabled_set:
        disabled = True
        reasons.append(f"Skill '{spec.name}' not in enabled set")

    meta = spec.metadata
    if meta:
        if meta.os and ctx.os_name and ctx.os_name not in meta.os:
            wrong_os = True
            reasons.append(f"OS mismatch: requires {', '.join(meta.os)}, running {ctx.os_name}")

        if meta.requires:
            for b in meta.requires.bins:
                if not _has_bin(b, ctx):
                    missing_bins.append(b)
                    reasons.append(f"Missing binary: {b}")

            if meta.requires.any_bins:
                if not any(_has_bin(b, ctx) for b in meta.requires.any_bins):
                    for b in meta.requires.any_bins:
                        if not _has_bin(b, ctx):
                            missing_bins.append(b)
                    reasons.append(f"Need one of: {', '.join(meta.requires.any_bins)}")

            for e in meta.requires.env:
                if not _has_env(e, ctx):
                    missing_env.append(e)
                    reasons.append(f"Missing env var: {e}")

            if meta.requires.env_any:
                if not any(_has_env(e, ctx) for e in meta.requires.env_any):
                    missing_env_any.append(list(meta.requires.env_any))
                    reasons.append(f"Need one env var from: {', '.join(meta.requires.env_any)}")

    # Match missing bins against install specs to produce hints
    install_hints: list[InstallHint] = []
    if meta and missing_bins:
        for ispec in meta.install:
            if ispec.bins and any(b in missing_bins for b in ispec.bins):
                cmd = _render_install_command(ispec)
                if cmd:
                    install_hints.append(
                        InstallHint(
                            kind=ispec.kind,
                            label=ispec.label or f"Install via {ispec.kind}",
                            command=cmd,
                        )
                    )

    return EligibilityReport(
        eligible=False,
        reasons=reasons,
        missing_bins=missing_bins,
        missing_env=missing_env,
        missing_env_any=missing_env_any,
        install_hints=install_hints,
        disabled=disabled,
        wrong_os=wrong_os,
        declared=_is_declared(spec),
    )


# ---------------------------------------------------------------------------
# Coding-mode availability
# ---------------------------------------------------------------------------
# Skills whose availability is governed by the operator "coding mode" toggle
# rather than the generic disabled list. code-task (the coding plugin) is
# available ONLY when coding mode is ON; turning coding mode off makes it
# unreachable through every skill API.
CODING_MODE_SKILLS: frozenset[str] = frozenset({"code-task"})


def effective_disabled(disabled: set[str] | list[str] | None, coding_mode: bool) -> set[str]:
    """The set of skill names to gate, given the operator config.

    Coding mode is AUTHORITATIVE for the coding-mode skills (code-task):
    when ON they are available regardless of the ``disabled`` list (so an
    upgraded user whose old toggle persisted "code-task" in ``disabled`` can
    still enable coding mode); when OFF they are gated regardless of it. All
    other skills follow the explicit ``disabled`` list.
    """
    result = set(disabled or ())
    if coding_mode:
        result -= CODING_MODE_SKILLS
    else:
        result |= CODING_MODE_SKILLS
    return result


def is_skill_available(
    name: str, *, disabled: set[str] | list[str] | None, coding_mode: bool
) -> bool:
    """Whether a skill may be surfaced or invoked under the operator config.

    Single source of truth used by every skill surface (skill list/view,
    the pre-turn skill gate) so coding mode cannot be bypassed via one path.
    """
    return name not in effective_disabled(disabled, coding_mode)


# ---------------------------------------------------------------------------
# Live operator gate (shared by every skill-reach path)
# ---------------------------------------------------------------------------
# Set once at gateway boot to a callable returning the live skills config, so
# coding-mode / disabled changes take effect immediately. Used by the skill
# tools AND the meta-skill executors so a disabled/coding-gated skill cannot be
# reached through any path (codex review).
_live_skills_cfg_getter: object | None = None


def set_live_skills_config_getter(getter: object | None) -> None:
    """Register the live skills-config getter (called by gateway boot)."""
    global _live_skills_cfg_getter
    _live_skills_cfg_getter = getter


def is_skill_available_live(name: str) -> bool:
    """Whether ``name`` is available under the CURRENT operator config.

    When no getter is registered (gate un-wired — degraded boot or a unit test),
    coding-mode skills FAIL CLOSED: code-task is gated, because OFF is the
    default and the safe state. All other skills remain available so the
    un-wired case never gates ordinary skills (codex review).
    """
    if _live_skills_cfg_getter is None:
        return name not in CODING_MODE_SKILLS
    cfg = _live_skills_cfg_getter()  # type: ignore[operator]
    disabled = getattr(cfg, "disabled", None) or []
    coding_mode = bool(getattr(cfg, "coding_mode", False))
    return is_skill_available(name, disabled=disabled, coding_mode=coding_mode)
