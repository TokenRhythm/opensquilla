"""Step 3: Gate skills deterministically, optionally filter by relevance, inject."""

from __future__ import annotations

import threading
from typing import Any, cast

import structlog

from opensquilla.engine.pipeline import TurnContext
from opensquilla.skills.eligibility import EligibilityContext, check_eligibility
from opensquilla.skills.retrieval import HybridRetriever, Strategy
from opensquilla.skills.types import SkillSpec

log = structlog.get_logger(__name__)

_retriever: HybridRetriever | None = None
_retriever_lock = threading.Lock()
_elig_ctx = EligibilityContext.auto()
_elig_ctx_lock = threading.RLock()
_elig_catalog_key: tuple[int, int] | None = None


def invalidate_skill_eligibility_cache() -> None:
    """Forget runtime bin/env probes after a dependency mutation succeeds."""
    with _elig_ctx_lock:
        _elig_ctx.has_bin_cache.clear()
        _elig_ctx.env_cache.clear()


def _sync_skill_eligibility_generation(catalog: Any) -> None:
    """Re-probe environment requirements when a new catalog is published."""
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


def _get_retriever(skills_cfg: Any) -> HybridRetriever:
    """Return a process-wide HybridRetriever sized to current config.
    Recreate when retrieval-shaping fields change so that tuning takes
    effect on next turn."""
    global _retriever
    rrf_k = getattr(skills_cfg, "filter_rrf_k", 60)
    lex_top_n = getattr(skills_cfg, "filter_lexical_top_n", 20)
    sem_top_n = getattr(skills_cfg, "filter_semantic_top_n", 20)
    model_name = getattr(skills_cfg, "filter_embedding_model", None)
    strategy = cast(Strategy, getattr(skills_cfg, "filter_strategy", "lexical"))
    config_key = (rrf_k, lex_top_n, sem_top_n, model_name, strategy)
    with _retriever_lock:
        if _retriever is None or getattr(_retriever, "_config_key", None) != config_key:
            from opensquilla.skills.retrieval.embedder import get_embedder

            # strategy="lexical" never needs an embedder; skip the lookup
            # so a missing skill-filter extra is not even attempted.
            embedder = None
            if strategy != "lexical" and model_name:
                try:
                    embedder = get_embedder(model_name)
                except ImportError:
                    embedder = None
            r = HybridRetriever(
                embedder=embedder,
                rrf_k=rrf_k,
                lexical_top_n=lex_top_n,
                semantic_top_n=sem_top_n,
                strategy=strategy,
            )
            setattr(r, "_config_key", config_key)
            _retriever = r
        return _retriever


def _eligibility_ctx(skills_cfg: Any) -> EligibilityContext:
    """Build the eligibility context, honoring config-disabled skills.

    ``skills.disabled`` lets an operator turn a skill off (e.g. from the
    control-UI toggle). Coding mode additionally gates the coding-mode skills
    (code-task) when it is OFF, via the shared ``effective_disabled`` helper.
    An empty result (the default: no disabled skills + coding mode off only
    gates code-task) is handled below.
    """
    from opensquilla.skills.eligibility import effective_disabled

    disabled = getattr(skills_cfg, "disabled", None) or []
    coding_mode = bool(getattr(skills_cfg, "coding_mode", False))
    effective = effective_disabled(disabled, coding_mode)
    if effective == _elig_ctx.disabled_set:
        return _elig_ctx
    # Derive from the warmed base context so the bin/env detection caches (and
    # any test monkeypatch of ``_elig_ctx``) are preserved — only the gated set
    # changes. Building a bare ``.auto()`` here would re-probe the environment
    # and wrongly gate env/bin-dependent skills.
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
    """Pure-Python gate: eligibility, requires_tools, fallback, visibility."""
    ctx_elig = elig_ctx or _elig_ctx
    gated: list[SkillSpec] = []
    with _elig_ctx_lock:
        for s in skills:
            if s.disable_model_invocation:
                continue
            if not check_eligibility(s, ctx_elig):
                continue
            if s.requires_tools and not all(t in available_tools for t in s.requires_tools):
                continue
            if s.fallback_for_toolsets and any(
                t in available_tools for t in s.fallback_for_toolsets
            ):
                continue
            gated.append(s)
    return gated


async def filter_skills(ctx: TurnContext) -> TurnContext:
    """Gate, optionally filter, and inject skills into the system prompt.

    Note: a ``meta_match`` in metadata used to short-circuit this step
    when hard-takeover was active. The takeover branch was removed and
    the trigger is now a *soft hint* injected by ``meta_resolution``, so
    we never bypass skill injection here — the meta-skill in question
    must still appear in ``<available_skills>`` so that the LLM can call
    ``meta_invoke(name=...)`` for it. The hinted skill is pinned below
    to defend against retrieval filters dropping it on noisy turns.
    """
    tools_cfg = getattr(ctx.config, "tools", None) if ctx.config else None
    if getattr(tools_cfg, "profile", None) == "memory_only":
        ctx.metadata["filtered_skill_ids"] = []
        ctx.metadata["skill_count"] = 0
        ctx.metadata["skills_prompt_chars"] = 0
        log.debug("skills_filter.skipped", reason="memory_only")
        return ctx

    catalog = getattr(ctx, "skill_catalog", None)
    if catalog is not None:
        _sync_skill_eligibility_generation(catalog)
        all_skills = list(getattr(catalog, "skills", ()))
    else:
        skill_loader = ctx.metadata.get("skill_loader")
        if skill_loader is None:
            return ctx
        all_skills = skill_loader.load_all()
    if not all_skills:
        return ctx

    from opensquilla.skills.meta.enabled import (
        is_meta_auto_trigger_enabled,
        is_meta_skill_enabled,
    )

    meta_skill_enabled = is_meta_skill_enabled(ctx.config)
    meta_auto_trigger = is_meta_auto_trigger_enabled(ctx.config)
    ctx.metadata["meta_skill_enabled"] = meta_skill_enabled

    # ── deterministic gate (no LLM, pure Python) ──
    available_tools = {t.name for t in ctx.tool_defs} if ctx.tool_defs else set()
    skills_cfg_for_gate = getattr(ctx.config, "skills", None) if ctx.config else None
    gated = _deterministic_gate(all_skills, available_tools, _eligibility_ctx(skills_cfg_for_gate))
    # Hide meta-skills from the model whenever auto-trigger is off (manual-only
    # mode) or the subsystem is fully disabled. They remain in the loader so the
    # /meta command can still enumerate and run them.
    if not (meta_skill_enabled and meta_auto_trigger):
        gated = [s for s in gated if getattr(s, "kind", "skill") != "meta"]
        for key in (
            "meta_match",
            "meta_match_trigger",
            "meta_match_candidates",
        ):
            ctx.metadata.pop(key, None)

    # ── always skills bypass filter, guaranteed visibility ──
    pinned = [s for s in gated if s.always]
    filterable = [s for s in gated if not s.always]

    # ── pin the meta-skill that meta_resolution matched ──
    # The soft-hint in system_prompt references this skill by name; if the
    # retriever (when filter_enabled=True) drops it from `<available_skills>`,
    # the LLM can still call `meta_invoke(name=...)` from memory, but won't
    # see the description block. Promote it to pinned to guarantee both.
    meta_match = ctx.metadata.get("meta_match")
    if meta_match is not None:
        hinted_name = getattr(getattr(meta_match, "plan", None), "name", None)
        if hinted_name:
            already_pinned = any(getattr(s, "name", None) == hinted_name for s in pinned)
            if not already_pinned:
                promoted = [s for s in filterable if getattr(s, "name", None) == hinted_name]
                if promoted:
                    pinned = pinned + promoted
                    filterable = [s for s in filterable if getattr(s, "name", None) != hinted_name]

    # ── pin explicitly requested skills (e.g. code-task under coding mode) ──
    for pin_name in ctx.metadata.get("pinned_skills", []) or []:
        if any(getattr(s, "name", None) == pin_name for s in pinned):
            continue
        promoted = [s for s in filterable if getattr(s, "name", None) == pin_name]
        if promoted:
            pinned = pinned + promoted
            filterable = [s for s in filterable if getattr(s, "name", None) != pin_name]

    skills_cfg = getattr(ctx.config, "skills", None) if ctx.config else None
    filter_enabled = getattr(skills_cfg, "filter_enabled", False) if skills_cfg else False
    max_chars = getattr(skills_cfg, "max_skills_prompt_chars", 8000)
    injection_mode = getattr(skills_cfg, "injection_mode", "system")
    semantic_message = getattr(ctx, "semantic_message", None)
    if semantic_message is None:
        semantic_message = getattr(ctx, "raw_message", None)
    if semantic_message is None:
        semantic_message = ctx.message

    # ── pin the meta-skill that meta_resolution matched ──
    # The soft-hint in system_prompt references this skill by name; if the
    # retriever (filter_enabled=True path) drops it from `<available_skills>`,
    # the LLM can still call `meta_invoke(name=...)` from memory, but won't
    # see the description block. Promote it to pinned to guarantee both.
    #
    # The meta match remains a soft hint: pin the matched workflow into
    # <available_skills>, but leave the outer tool surface intact so the LLM
    # can make the final semantic judgment about whether to call meta_invoke.
    meta_match = ctx.metadata.get("meta_match")
    if meta_match is not None:
        hinted_name = getattr(getattr(meta_match, "plan", None), "name", None)
        if hinted_name:
            already_pinned = any(getattr(s, "name", None) == hinted_name for s in pinned)
            if not already_pinned:
                promoted = [s for s in filterable if getattr(s, "name", None) == hinted_name]
                if promoted:
                    pinned = pinned + promoted
                    filterable = [s for s in filterable if getattr(s, "name", None) != hinted_name]

    if filter_enabled:
        top_k = getattr(skills_cfg, "filter_top_k", 5)
        # Strategy ("lexical" / "semantic" / "hybrid") is handled inside
        # HybridRetriever — see opensquilla.skills.retrieval.HybridRetriever.
        retriever = _get_retriever(skills_cfg)
        filtered = retriever.retrieve(filterable, semantic_message, top_k=top_k)
    else:
        filtered = filterable

    final = pinned + filtered

    # Publish the post-filter skill-ID list so the pipeline wrapper can
    # surface it in the decision log's PipelineStepRecord. Non-mutating
    # additive read for callers that don't consume the metadata.
    try:
        ctx.metadata["filtered_skill_ids"] = [
            getattr(s, "id", None) or getattr(s, "name", None)
            for s in filtered
            if getattr(s, "id", None) or getattr(s, "name", None)
        ]
    except Exception:  # pragma: no cover — metadata is best-effort
        ctx.metadata["filtered_skill_ids"] = []

    from opensquilla.skills.injector import SkillInjector

    injector = SkillInjector()
    # tuple[1] is the uncached suffix slot: may already carry the
    # per-turn recalled-memory block produced upstream by
    # TurnRunner._assemble_prompt. Append instead of overwriting so that
    # both recall and skills survive the pipeline together.
    if isinstance(ctx.system_prompt, str):
        base, suffix = ctx.system_prompt, ""
    else:
        base, suffix = ctx.system_prompt

    if injection_mode == "user_message":
        skills_prompt = injector.inject_compact("", final)
    elif injection_mode == "user_context":
        skills_prompt = injector.inject_skills(
            "", final, max_chars=max_chars, pinned_count=len(pinned)
        )
    else:
        skills_prompt = injector.inject_skills(
            "", final, max_chars=max_chars, pinned_count=len(pinned)
        )
    ctx.metadata["skill_count"] = len(final)
    # The selected count (``skill_count``) is pre-injection; under a tight budget
    # inject_skills can degrade to a name-only prefix that renders FEWER entries.
    # Record the count actually written into <available_skills> so recall debugging
    # reflects what the model really saw, not what the filter selected. Count the
    # closing </name> tag — the opening <name> substring also appears in the meta
    # guidance prose (meta_invoke(name="<name>")), which would over-count.
    ctx.metadata["skills_rendered_count"] = skills_prompt.count("</name>")
    ctx.metadata["skills_prompt_chars"] = len(skills_prompt)
    ctx.metadata["skills_injection_mode"] = injection_mode

    # Surface the actual skill IDs the retriever picked. Without this,
    # operators can see a query passed through (total → filtered count)
    # but cannot tell which skills were chosen vs missed — the diagnostic
    # signal needed to debug recall quality (e.g. "why did 'commit my
    # changes to git' not surface `git`?").
    pinned_ids = [
        getattr(s, "id", None) or getattr(s, "name", None)
        for s in pinned
        if getattr(s, "id", None) or getattr(s, "name", None)
    ]
    filtered_ids = ctx.metadata.get("filtered_skill_ids") or []
    log.debug(
        "skills_filter.applied",
        total=len(all_skills),
        gated=len(gated),
        pinned=len(pinned),
        filtered=len(final),
        mode=injection_mode,
        strategy=getattr(skills_cfg, "filter_strategy", "lexical") if filter_enabled else "off",
        query_preview=(
            semantic_message[:60] + "..."
            if isinstance(semantic_message, str) and len(semantic_message) > 60
            else semantic_message
        ),
        pinned_skills=pinned_ids,
        filtered_skills=filtered_ids,
    )

    if skills_prompt and injection_mode == "user_context":
        ctx.metadata["skills_context_prompt"] = skills_prompt
    elif skills_prompt:
        combined_suffix = f"{suffix}\n\n{skills_prompt}" if suffix else skills_prompt
        ctx.system_prompt = (base, combined_suffix)
    # else: leave ctx.system_prompt unchanged — preserves any upstream tuple
    # carrying the recall block, or stays str when neither is present.

    return ctx
