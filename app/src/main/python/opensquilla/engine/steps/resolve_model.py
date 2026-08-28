"""Step 1: Resolve model from config."""

from __future__ import annotations

import structlog

from opensquilla.engine.pipeline import TurnContext

log = structlog.get_logger(__name__)


async def resolve_model(ctx: TurnContext) -> TurnContext:
    """Read model from config.llm.model and apply to context."""
    if ctx.config is None:
        return ctx
    llm_cfg = getattr(ctx.config, "llm", None)
    if llm_cfg and llm_cfg.model:
        ctx.model = llm_cfg.model
        log.debug("resolve_model.applied", model=ctx.model)
    return ctx
