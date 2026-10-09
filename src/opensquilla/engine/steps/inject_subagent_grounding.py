"""Re-inject the subagent grounding system prompt every turn.

Compaction can drop the early user message that originally carried the child
execution contract. This pipeline step ensures the contract remains present
in every turn of a recognized delegated-agent session.

The check is idempotent: if the marker is already present anywhere in the
system prompt, the step is a no-op. The injection happens before
``apply_prompt_cache`` so the grounding becomes part of the cacheable
prefix.
"""

from __future__ import annotations

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.subagent_delegation import (
    SINGLE_SUBAGENT_EXECUTION_PROMPT,
    SUBAGENT_EXECUTION_PROMPT,
)
from opensquilla.session.keys import is_subagent_key

_SUBAGENT_GROUNDING = SUBAGENT_EXECUTION_PROMPT


def _system_prompt_contains_grounding(
    prompt: str | tuple[str, str], grounding: str = _SUBAGENT_GROUNDING
) -> bool:
    if isinstance(prompt, tuple):
        return any(grounding in part for part in prompt if isinstance(part, str))
    return isinstance(prompt, str) and grounding in prompt


def _prepend_grounding(
    prompt: str | tuple[str, str], grounding: str = _SUBAGENT_GROUNDING
) -> str | tuple[str, str]:
    if isinstance(prompt, tuple):
        cacheable, dynamic = prompt
        new_dynamic = f"{grounding}\n\n{dynamic}" if dynamic else grounding
        return (cacheable, new_dynamic)
    if isinstance(prompt, str):
        if not prompt:
            return grounding
        return f"{grounding}\n\n{prompt}"
    # Unknown shape — leave untouched rather than corrupt.
    return prompt


async def inject_subagent_grounding(ctx: TurnContext) -> TurnContext:
    """Idempotently re-inject grounding for recognized subagent sessions."""
    session_key = ctx.session_key or ""
    if not is_subagent_key(session_key):
        ctx.metadata["inject_subagent_grounding__applied"] = False
        return ctx
    grounding = (
        SINGLE_SUBAGENT_EXECUTION_PROMPT
        if ctx.metadata.get("single_agent_mode")
        else _SUBAGENT_GROUNDING
    )
    if _system_prompt_contains_grounding(ctx.system_prompt, grounding):
        ctx.metadata["inject_subagent_grounding__applied"] = False
        return ctx
    ctx.system_prompt = _prepend_grounding(ctx.system_prompt, grounding)
    ctx.metadata["inject_subagent_grounding__applied"] = True
    return ctx
