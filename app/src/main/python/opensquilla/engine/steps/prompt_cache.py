"""Step 4: annotate system prompt with provider cache breakpoints."""

from __future__ import annotations

import hashlib

import structlog

from opensquilla.engine.pipeline import TurnContext
from opensquilla.session.keys import parse_agent_id

log = structlog.get_logger(__name__)

_LEGACY_TO_SHADOW: dict[str, str] = {}


def _hash16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _record_prompt_cache_metrics(ctx: TurnContext, *, base: str, dynamic: str = "") -> None:
    ctx.metadata["cache_base_chars"] = len(base)
    ctx.metadata["cache_base_hash"] = _hash16(base)
    if dynamic:
        ctx.metadata["cache_dynamic_chars"] = len(dynamic)
        ctx.metadata["cache_dynamic_hash"] = _hash16(dynamic)


def _record_dual_track_key_metrics(ctx: TurnContext) -> None:
    agent_id = parse_agent_id(ctx.session_key)
    resolved_model = ctx.model or ""
    provider = ctx.provider
    provider_after_rewrite = (
        getattr(provider, "provider_name", None)
        or getattr(getattr(ctx.config, "llm", None), "provider", "")
        or ""
    )
    channel_pinned = bool(ctx.metadata.get("platform_markdown_hint"))
    legacy_hash = _hash16(f"{agent_id}\0{resolved_model}")
    shadow_hash = _hash16(
        f"{agent_id}\0{resolved_model}\0{provider_after_rewrite}\0{channel_pinned}"
    )
    previous = _LEGACY_TO_SHADOW.get(legacy_hash)
    collision = previous is not None and previous != shadow_hash
    _LEGACY_TO_SHADOW.setdefault(legacy_hash, shadow_hash)

    ctx.metadata["resolved_model"] = resolved_model
    ctx.metadata["alias_resolution_chain"] = [resolved_model] if resolved_model else []
    ctx.metadata["provider_after_rewrite"] = str(provider_after_rewrite)
    ctx.metadata["cache_legacy_hash"] = legacy_hash
    ctx.metadata["cache_shadow_final_hash"] = shadow_hash
    ctx.metadata["cache_key_collision"] = collision


async def apply_prompt_cache(ctx: TurnContext) -> TurnContext:
    """Mark system prompt for Anthropic prompt caching (best-effort via OpenRouter)."""
    cache_cfg = getattr(ctx.config, "prompt_cache", None) if ctx.config else None
    if not cache_cfg:
        return ctx

    mode = getattr(cache_cfg, "effective_mode", "off")
    if mode == "off":
        return ctx

    log.debug("prompt_cache.applying")
    ctx.metadata["cache_enabled"] = True
    ctx.metadata["cache_mode"] = mode
    _record_dual_track_key_metrics(ctx)

    if isinstance(ctx.system_prompt, tuple) and len(ctx.system_prompt) == 2:
        base, dynamic = ctx.system_prompt
        ctx.metadata["cache_base_prompt"] = base
        ctx.metadata["cache_dynamic_prompt"] = dynamic
        _record_prompt_cache_metrics(ctx, base=base, dynamic=dynamic)
    elif isinstance(ctx.system_prompt, str) and ctx.system_prompt:
        ctx.metadata["cache_base_prompt"] = ctx.system_prompt
        _record_prompt_cache_metrics(ctx, base=ctx.system_prompt)

    if ctx.tool_defs:
        ctx.metadata["cache_last_tool"] = True

    return ctx
