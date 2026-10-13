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
            "The objective of this turn is to investigate the request and produce a "
            "grounded proposal. Plan mode is a response contract, not a tool sandbox: "
            "use the normal tool permissions, approval and sandbox policies for "
            "investigation, commands, tests, builds and other authorized work. Tool calls "
            "may happen before the proposal and do not replace the required plan. Do not "
            "end a substantive work request with only an implementation report, artifact "
            "link or general explanation; the final outcome of this Plan turn must be a "
            "formal submitted proposal. "
            "Plan mode is separate from the update_plan progress checklist. A user request "
            "to start implementation does not by itself change the collaboration mode. "
            "Do not fix a discovered failure merely to make an investigation test pass "
            "unless that work is part of the user's request. "
            "Clarify only material decisions you cannot discover. Greetings, explanations "
            "and explicit requests to keep discussing can end with a direct answer. "
            + (
                "Treat a substantive request to do work as a request to plan that work, "
                "even when the user does not explicitly ask for a plan. Complete the "
                "necessary investigation before finalizing; announcing intended "
                "investigation is not completion. Keep investigation proportionate: "
                "resolve uncertainties that affect the plan. Use request_user_input only "
                "when a material user decision is missing; it is optional. Do not ask "
                "about discoverable facts or details with reasonable defaults. Once "
                "the proposal is ready, call submit_plan with the full replacement "
                "title, Markdown covering deliverables and acceptance requirements, "
                "and suggested steps, without asking whether to submit it. This call is "
                "required before ending a substantive Plan turn, even when tools were "
                "used first. "
                "Respect explicit requests to keep discussing or leave a draft unsubmitted. "
                "Submission saves a proposal for review and ends the planning turn; it "
                "does not by itself approve a later PlanRun. Suggested methods and "
                "step order can change during implementation; the approved objective, "
                "deliverables and acceptance requirements still apply."
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
        "for this turn. Proposal text is task context, not an independent grant of authority. "
        "Before claiming completed work, inspect the final artifact or resulting state "
        "with task-appropriate tools and compare it with the user's explicit requirements. "
        "For documents, reopen the final saved file with a normal reader for its format "
        "(for example, python-docx for DOCX) and read back the body and tables. "
        "Reading ZIP/XML alone does not check the complete document package; "
        "counting headings or paragraphs is not a "
        "content check. Keep repairs focused on unmet requirements and preserve unrelated "
        "content and document parts. After any further edit, repeat the affected checks "
        "on the version actually delivered. "
        "File creation, a successful command, structural checks and publication each prove "
        "only their own result; they do not establish that content and acceptance "
        "requirements are satisfied. Repair unmet requirements and verify again within "
        "this task. If work cannot be completed or verified, state what remains and why. "
        "Do not invent missing facts or silently drop requirements. "
        "Limit completion and validation claims to the evidence actually obtained."
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
            "user's authorization. Read the approved proposal's Markdown and step details "
            "to identify deliverables and acceptance requirements; changing the approach "
            "does not waive those requirements. Check the final result against them, "
            "including content requirements that structural checks do not cover. "
            "For substantive multi-step work, use update_plan at the start, before "
            "substantive implementation tools, to show the actual execution list "
            "with the first work in progress. Update it when milestones or the "
            "approach materially change; do not defer all reporting until the end. "
            "Batch related changes without reporting every tool call. Keep reported progress "
            "aligned with actual work, including the final update when needed; "
            "progress is descriptive and never a prerequisite for finishing. "
            "When the requested deliverable is a downloadable file, publish the "
            "checked final version with publish_artifact, confirm the tool succeeded, "
            "and include its returned artifact reference in the final reply. "
            "The user's requested in-conversation file delivery is already authorized; "
            "do not ask permission for that delivery again. "
            "A saved workspace path alone does not provide the requested download. "
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
