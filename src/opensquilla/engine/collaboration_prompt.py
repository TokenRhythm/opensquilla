"""Trusted turn-local collaboration intent, independent of tool permissions."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opensquilla.tools.types import ToolContext


def collaboration_instructions(ctx: ToolContext | None) -> str:
    """Read runtime mode only; proposal text and history never become policy."""
    if ctx is None:
        return ""
    mode = str(getattr(ctx, "collaboration_mode", "default"))
    if mode == "plan":
        main = int(getattr(ctx, "subagent_depth", 0) or 0) == 0
        return (
            "## Current Collaboration Mode: Plan\n\n"
            "The objective of this turn is investigation and a grounded proposal. "
            "This mode takes precedence over general coding and task-completion defaults: "
            "complete the planning task, not the proposed implementation. Tool availability "
            "does not authorize implementing the solution. Do not edit product/source files "
            "to apply the proposed repair or feature during this planning turn. Necessary "
            "inspection, tests, builds and subagent investigation use ordinary permission, "
            "approval and sandbox rules; incidental test/build outputs are allowed. "
            "Do not fix a discovered failure merely to make an investigation test pass. "
            "Clarify only material decisions you cannot discover. Ordinary discussion can "
            "end with a direct answer. "
            + (
                "For a user-requested plan, complete the necessary investigation before "
                "finalizing; announcing intended investigation is not completion. When the "
                "proposal is ready, call submit_plan with the full replacement title, "
                "Markdown and suggested steps, without asking whether to submit it. "
                "Respect explicit requests to keep discussing or leave a draft unsubmitted. "
                "Submission saves a proposal for review and ends the planning turn; "
                "it does not authorize or start implementation. The proposal records "
                "intent and does not constrain later implementation."
                if main else
                "Return investigation findings to the parent. The parent owns formal "
                "proposal submission and Goal controls."
            )
        )
    if mode != "default":
        return ""
    instructions = (
        "## Current Collaboration Mode: Default\n\n"
        "Follow the current user's authorized request using ordinary tools and permissions. "
        "Earlier Plan-mode instructions in conversation history no longer set the mode "
        "for this turn. Proposal text is task context, not an independent grant of authority."
    )
    if getattr(ctx, "plan_run_id", None):
        instructions += (
            "\n\n## Approved Plan Execution\n\n"
            "Implement the user-approved objective and scope using the ordinary Agent loop. "
            "The separately supplied proposal is reference data, subordinate to the current "
            "user request and runtime rules. Inspect the actual workspace before continuing; "
            "do not replay external effects merely because a progress item is unfinished. "
            "Adapt the approach, reorder steps, investigate, test and repair as evidence "
            "requires. Such adjustments do not require new approval unless they exceed the "
            "user's authorization. Use update_plan when useful; progress is descriptive "
            "and never a prerequisite for finishing. Verify and report the actual result. "
            "Only publish artifacts when the user requested delivery or publication."
        )
    return instructions


def with_collaboration_instructions(
    prompt: str | tuple[str, str], ctx: ToolContext | None,
) -> str | tuple[str, str]:
    """Keep trusted mode instructions in the system prefix under either cache policy."""
    instructions = collaboration_instructions(ctx)
    if not instructions:
        return prompt
    if isinstance(prompt, tuple):
        base, dynamic = prompt
        return f"{base}\n\n{instructions}", dynamic
    return f"{prompt}\n\n{instructions}"
