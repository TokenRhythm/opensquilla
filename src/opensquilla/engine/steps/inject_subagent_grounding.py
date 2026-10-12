"""Re-inject the subagent grounding system prompt every turn.

Compaction can drop the early user message that originally carried the
``_SUBAGENT_SYSTEM_PROMPT`` grounding text (sessions.py wraps it into the
first user turn). This pipeline step is a fallback that ensures the
grounding text is present in the system prompt for every turn of any
recognized subagent session key.

The check is idempotent: if the marker is already present anywhere in the
system prompt, the step is a no-op. The injection happens before
``apply_prompt_cache`` so the grounding becomes part of the cacheable
prefix.
"""

from __future__ import annotations

from opensquilla.engine.pipeline import TurnContext
from opensquilla.session.keys import is_subagent_key

# Kept in lock-step with tools.builtin.sessions._SUBAGENT_SYSTEM_PROMPT.
# If those texts diverge the fallback test will catch the mismatch.
_SUBAGENT_GROUNDING = (
    "You are a subagent. Execute the delegated task faithfully and return "
    "a structured result to your parent session."
)


def _system_prompt_contains_grounding(prompt: str | tuple[str, str]) -> bool:
    if isinstance(prompt, tuple):
        return any(_SUBAGENT_GROUNDING in part for part in prompt if isinstance(part, str))
    return isinstance(prompt, str) and _SUBAGENT_GROUNDING in prompt


def _prepend_grounding(prompt: str | tuple[str, str]) -> str | tuple[str, str]:
    if isinstance(prompt, tuple):
        cacheable, dynamic = prompt
        new_dynamic = f"{_SUBAGENT_GROUNDING}\n\n{dynamic}" if dynamic else _SUBAGENT_GROUNDING
        return (cacheable, new_dynamic)
    if isinstance(prompt, str):
        if not prompt:
            return _SUBAGENT_GROUNDING
        return f"{_SUBAGENT_GROUNDING}\n\n{prompt}"
    # Unknown shape — leave untouched rather than corrupt.
    return prompt


async def inject_subagent_grounding(ctx: TurnContext) -> TurnContext:
    """Idempotently re-inject grounding for recognized subagent sessions."""
    session_key = ctx.session_key or ""
    if not is_subagent_key(session_key):
        ctx.metadata["inject_subagent_grounding__applied"] = False
        return ctx
    if _system_prompt_contains_grounding(ctx.system_prompt):
        ctx.metadata["inject_subagent_grounding__applied"] = False
        return ctx
    ctx.system_prompt = _prepend_grounding(ctx.system_prompt)
    ctx.metadata["inject_subagent_grounding__applied"] = True
    return ctx
