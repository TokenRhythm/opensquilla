"""Resolve the deterministic, generation-pinned model Skill catalog."""

from __future__ import annotations

import threading
from typing import Any

import structlog

from opensquilla.engine.pipeline import TurnContext
from opensquilla.skills.catalog_policy import project_public_catalog
from opensquilla.skills.eligibility import EligibilityContext, check_eligibility
from opensquilla.skills.types import SkillSpec

log = structlog.get_logger(__name__)

_elig_ctx = EligibilityContext.auto()
_elig_ctx_lock = threading.RLock()
_elig_catalog_key: tuple[int, int] | None = None


def invalidate_skill_eligibility_cache() -> None:
    """Forget runtime bin/env probes after a dependency mutation succeeds."""

    with _elig_ctx_lock:
        _elig_ctx.has_bin_cache.clear()
        _elig_ctx.env_cache.clear()


def _sync_skill_eligibility_generation(catalog: Any) -> None:
    generation = getattr(catalog, "generation", None)
    if not isinstance(generation, int):
        return
    key = (id(catalog), generation)
    global _elig_catalog_key
    with _elig_ctx_lock:
        if key == _elig_catalog_key:
            return
        _elig_ctx.has_bin_cache.clear()
        _elig_ctx.env_cache.clear()
        _elig_catalog_key = key


def _eligibility_ctx(skills_cfg: Any) -> EligibilityContext:
    from opensquilla.skills.eligibility import effective_disabled

    disabled = getattr(skills_cfg, "disabled", None) or []
    coding_mode = bool(getattr(skills_cfg, "coding_mode", False))
    effective = effective_disabled(disabled, coding_mode)
    if effective == _elig_ctx.disabled_set:
        return _elig_ctx
    return EligibilityContext(
        os_name=_elig_ctx.os_name,
        has_bin_cache=_elig_ctx.has_bin_cache,
        env_cache=_elig_ctx.env_cache,
        enabled_set=_elig_ctx.enabled_set,
        disabled_set=set(effective),
    )


def _deterministic_gate(
    skills: list[SkillSpec],
    available_tools: set[str],
    elig_ctx: EligibilityContext | None = None,
) -> list[SkillSpec]:
    """Apply only capability, dependency, and explicit product-mode gates."""

    resolved = elig_ctx or _elig_ctx
    gated: list[SkillSpec] = []
    with _elig_ctx_lock:
        for skill in skills:
            if skill.disable_model_invocation:
                continue
            if not check_eligibility(skill, resolved):
                continue
            if skill.requires_tools and not all(
                tool in available_tools for tool in skill.requires_tools
            ):
                continue
            if skill.fallback_for_toolsets and any(
                tool in available_tools for tool in skill.fallback_for_toolsets
            ):
                continue
            gated.append(skill)
    return gated


async def resolve_skill_catalog(ctx: TurnContext) -> TurnContext:
    """Project one stable public catalog and attach it to the prompt.

    This step never reads the current message, ranks by relevance, embeds text,
    or applies Top-K.  For a fixed catalog generation, model/tool profile, and
    product mode, the rendered bytes are deterministic.
    """

    tools_cfg = getattr(ctx.config, "tools", None) if ctx.config else None
    if getattr(tools_cfg, "profile", None) == "memory_only":
        ctx.metadata.update(
            skill_catalog_ids=[],
            skill_count=0,
            skills_rendered_count=0,
            skills_prompt_chars=0,
            skills_catalog_omitted_count=0,
        )
        return ctx

    catalog = getattr(ctx, "skill_catalog", None)
    if catalog is not None:
        _sync_skill_eligibility_generation(catalog)
        all_skills = list(getattr(catalog, "skills", ()))
        generation = int(getattr(catalog, "generation", 0) or 0)
    else:
        loader = ctx.metadata.get("skill_loader")
        if loader is None:
            return ctx
        snapshot = loader.snapshot_for_turn(reason="skill_catalog_projection")
        all_skills = list(getattr(snapshot, "skills", ()))
        generation = int(getattr(snapshot, "generation", 0) or 0)
    if not all_skills:
        return ctx

    from opensquilla.skills.meta.enabled import (
        is_meta_auto_trigger_enabled,
        is_meta_skill_enabled,
    )

    meta_enabled = is_meta_skill_enabled(ctx.config)
    meta_auto = is_meta_auto_trigger_enabled(ctx.config)
    ctx.metadata["meta_skill_enabled"] = meta_enabled
    if not (meta_enabled and meta_auto):
        for key in ("meta_match", "meta_match_trigger", "meta_match_candidates"):
            ctx.metadata.pop(key, None)

    skills_cfg = getattr(ctx.config, "skills", None) if ctx.config else None
    coding_mode = bool(getattr(skills_cfg, "coding_mode", False))
    available_tools = {tool.name for tool in ctx.tool_defs} if ctx.tool_defs else set()
    gated = _deterministic_gate(
        all_skills,
        available_tools,
        _eligibility_ctx(skills_cfg),
    )
    projected = project_public_catalog(
        gated,
        coding_mode=coding_mode,
        include_stable_meta=meta_enabled and meta_auto,
    )

    # An explicit, already-authorized mention may lead under a constrained
    # metadata budget. It never admits an otherwise internal Skill.
    pinned_names = [str(name) for name in (ctx.metadata.get("pinned_skills") or [])]
    if pinned_names:
        pinned = [skill for name in pinned_names for skill in projected if skill.name == name]
        pinned_ids = {id(skill) for skill in pinned}
        projected = [*pinned, *(skill for skill in projected if id(skill) not in pinned_ids)]
    else:
        pinned = []

    from opensquilla.skills.injector import SkillInjector

    injector = SkillInjector()
    max_chars = int(getattr(skills_cfg, "max_skills_prompt_chars", 8000) or 8000)
    injection_mode = str(getattr(skills_cfg, "injection_mode", "system") or "system")
    if isinstance(ctx.system_prompt, str):
        base, suffix = ctx.system_prompt, ""
    else:
        base, suffix = ctx.system_prompt

    if injection_mode == "user_message":
        rendered = injector.inject_compact("", projected, generation=generation)
    else:
        rendered = injector.inject_skills(
            "",
            projected,
            max_chars=max_chars,
            pinned_count=len(pinned),
            generation=generation,
        )
    report = injector.last_render_report
    ctx.metadata.update(
        skill_catalog_ids=[skill.name for skill in projected],
        skill_catalog_generation=generation,
        skill_count=len(projected),
        skills_rendered_count=report.rendered,
        skills_prompt_chars=len(rendered),
        skills_injection_mode=injection_mode,
        skills_catalog_omitted_count=report.omitted,
    )

    if rendered and injection_mode == "user_context":
        ctx.metadata["skills_context_prompt"] = rendered
    elif rendered:
        # The generation-stable catalog belongs in the cacheable prefix. The
        # recalled-memory/per-turn suffix stays in the dynamic slot.
        ctx.system_prompt = (f"{base}\n\n{rendered}", suffix)

    log.debug(
        "skill_catalog.projected",
        generation=generation,
        internal_total=len(all_skills),
        eligible_total=len(gated),
        public_total=len(projected),
        rendered=report.rendered,
        omitted=report.omitted,
        coding_mode=coding_mode,
        meta_auto=meta_enabled and meta_auto,
    )
    return ctx
