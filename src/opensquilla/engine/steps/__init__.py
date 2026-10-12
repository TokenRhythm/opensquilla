"""Pre-turn pipeline steps."""

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.steps.coding_mode import enforce_coding_mode
from opensquilla.engine.steps.inject_platform_hint import inject_platform_hint
from opensquilla.engine.steps.inject_subagent_grounding import inject_subagent_grounding
from opensquilla.engine.steps.meta_command import meta_command_launch
from opensquilla.engine.steps.meta_resolution import meta_resolution
from opensquilla.engine.steps.prompt_cache import apply_prompt_cache
from opensquilla.engine.steps.reasoning_hint_observer import observe_reasoning_hint
from opensquilla.engine.steps.resolve_model import resolve_model
from opensquilla.engine.steps.skills_filter import filter_skills
from opensquilla.engine.steps.vision_followup_gate import apply_vision_followup_gate

try:
    from opensquilla.engine.steps.squilla_router import apply_squilla_router
except ImportError:

    async def apply_squilla_router(ctx: TurnContext) -> TurnContext:
        return ctx


__all__ = [
    "apply_prompt_cache",
    "apply_squilla_router",
    "apply_vision_followup_gate",
    "enforce_coding_mode",
    "filter_skills",
    "inject_platform_hint",
    "inject_subagent_grounding",
    "meta_command_launch",
    "meta_resolution",
    "observe_reasoning_hint",
    "resolve_model",
]
