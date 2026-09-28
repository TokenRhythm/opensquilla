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
        if name.casefold() == "git":
            try:
                from opensquilla.git_runtime import resolve_git_capability

                result = resolve_git_capability().available
            except Exception:
                # The system Git path may be an unusable platform shim. Treat
                # capability-resolution failures as missing rather than
                # advertising a skill that cannot run.
                result = False
            ctx.has_bin_cache[name] = result
            return result
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


def effective_disabled(disabled: set[str] | list[str] | None) -> set[str]:
    """Skills are governed only by the explicit operator disabled list."""
    return set(disabled or ())


def eligibility_context_for_skills_config(config: object | None) -> EligibilityContext:
    return EligibilityContext.auto(
        disabled_set=effective_disabled(getattr(config, "disabled", None)),
    )


def is_skill_available(name: str, *, disabled: set[str] | list[str] | None) -> bool:
    return name not in effective_disabled(disabled)


_live_skills_cfg_getter: object | None = None


def set_live_skills_config_getter(getter: object | None) -> None:
    """Register the live skills-config getter (called by gateway boot)."""
    global _live_skills_cfg_getter
    _live_skills_cfg_getter = getter


def is_skill_available_live(name: str) -> bool:
    if _live_skills_cfg_getter is None:
        return True
    cfg = _live_skills_cfg_getter()  # type: ignore[operator]
    return is_skill_available(name, disabled=getattr(cfg, "disabled", None))


def live_eligibility_context(fallback_config: object | None = None) -> EligibilityContext:
    config = fallback_config
    if _live_skills_cfg_getter is not None:
        config = _live_skills_cfg_getter()  # type: ignore[operator]
    return eligibility_context_for_skills_config(config)
