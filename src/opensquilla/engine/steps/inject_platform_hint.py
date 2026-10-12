"""Inject channel-specific rendering hints into the dynamic prompt suffix."""

from __future__ import annotations

from opensquilla.engine.pipeline import TurnContext


async def inject_platform_hint(ctx: TurnContext) -> TurnContext:
    """Append a channel rendering hint to the uncached suffix when needed."""

    prompt_cfg = getattr(ctx.config, "prompt", None) if ctx.config else None
    if not getattr(prompt_cfg, "platform_hint_enabled", True):
        ctx.metadata["inject_platform_hint__applied"] = False
        return ctx

    from opensquilla.channels.registry import markdown_render_hint_for

    channel_kind = str(ctx.metadata.get("channel_kind") or "").strip().lower()
    hint = markdown_render_hint_for(channel_kind)
    if not hint:
        ctx.metadata["inject_platform_hint__applied"] = False
        return ctx

    if isinstance(ctx.system_prompt, str):
        base, suffix = ctx.system_prompt, ""
    else:
        base, suffix = ctx.system_prompt

    block = f"## Channel Rendering\n\n{hint}"
    ctx.system_prompt = (base, f"{suffix}\n\n{block}" if suffix else block)
    ctx.metadata["platform_markdown_hint"] = channel_kind
    return ctx
