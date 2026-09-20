"""Structured controls for one generation-fenced Goal turn."""

from __future__ import annotations

import json
from typing import Any

from opensquilla.tools.registry import tool
from opensquilla.tools.types import (
    RetryableToolInputError,
    SafeToolError,
    current_tool_context,
    is_goal_owned_main_default_turn,
)


def _goal_turn() -> tuple[Any, dict[str, Any]]:
    ctx = current_tool_context.get()
    if not is_goal_owned_main_default_turn(ctx):
        raise SafeToolError("Goal controls are unavailable in this turn.")
    assert ctx is not None
    context = getattr(ctx, "goal_context", None)
    service = getattr(ctx, "goal_service", None)
    if not isinstance(context, dict) or service is None:
        raise SafeToolError("This turn does not own an active Goal.")
    return service, dict(context)


def _optional_text(value: Any, *, field: str, max_chars: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_chars:
        raise RetryableToolInputError(f"{field} must be at most {max_chars} characters")
    return text


def _control_turn() -> Any:
    ctx = current_tool_context.get()
    if (
        ctx is None
        or ctx.subagent_depth
        or not ctx.is_owner
        or str(ctx.caller_kind) not in {"agent", "web", "cli"}
        or ctx.collaboration_mode != "default"
        or not ctx.task_id
        or ctx.goal_service is None
    ):
        raise SafeToolError("Goal controls require an owning main Default task.")
    return ctx


@tool(
    name="get_goal", description="Read the current task's persistent Goal, status and usage.",
    params={}, required=[], default_access="deny", terminates_turn=False,
)
async def get_goal() -> str:
    ctx = _control_turn()
    result = await ctx.goal_service.status(ctx.session_key)
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


@tool(
    name="create_goal",
    description=("Create a persistent Goal only when the user explicitly asks for one. "
                 "Reuse the current task. "
                 "An unfinished Goal must be continued rather than replaced."),
    params={"objective": {"type": "string", "minLength": 1, "maxLength": 4000}},
    required=["objective"], default_access="deny", terminates_turn=False,
)
async def create_goal(objective: str) -> str:
    ctx = _control_turn()
    try:
        result = await ctx.goal_service.create_from_turn(ctx, objective=objective)
    except Exception as exc:
        raise SafeToolError(str(exc)) from exc
    return json.dumps(
        {"status": "accepted", "goal": result}, ensure_ascii=False, separators=(",", ":")
    )


@tool(
    name="update_goal",
    description=(
        "Update the current session's Goal using this main task. Change the objective "
        "only when the user requests it. "
        "Edits preserve an unfinished Goal's state unless "
        "status is provided; combine an edit with paused to pause atomically, or active "
        "to resume. Editing a completed Goal reopens it. "
        "Use active only to resume at the user's request, "
        "continuing the current task. Use paused only at the user’s explicit "
        "request and stop Goal work; it does not cancel the current task. "
        "Use complete only after authoritative current evidence proves every requirement "
        "in the full objective and no requested work remains. If evidence is weak, indirect, "
        "incomplete, uncertain, or missing, keep working instead. Use blocked only after "
        "the same blocking condition has prevented meaningful progress in at least three "
        "consecutive Goal turns and work is at a true impasse without user input or an "
        "external-state change. A resumed previously blocked Goal starts a fresh blocked "
        "audit. Do not use blocked merely because work is hard, slow, uncertain, "
        "incomplete, or would benefit from clarification."
    ),
    params={
        "objective": {"type": "string", "minLength": 1, "maxLength": 4000},
        "status": {
            "type": "string",
            "enum": ["complete", "blocked", "paused", "active"],
            "description": (
                "Requested Goal state. active and paused may accompany objective "
                "edits. Submit complete or blocked separately from edits; complete requires "
                "proof of the full objective, and blocked requires the repeated-blocker "
                "and true-impasse conditions."
            ),
        },
        "reason": {
            "type": "string",
            "maxLength": 1000,
            "description": (
                "Required concise description of the repeatedly observed blocker for "
                "blocked; omit for every other status or edit."
            ),
        },
    },
    required=[],
    default_access="deny",
    terminates_turn=False,
)
async def update_goal(
    status: str | None = None,
    reason: str | None = None,
    objective: str | None = None,
) -> str:
    normalized = str(status).strip().lower() if status is not None else None
    if normalized not in {None, "complete", "blocked", "paused", "active"}:
        raise RetryableToolInputError("status must be active, complete, blocked, or paused")
    needs_pause_binding = normalized == "paused" and not is_goal_owned_main_default_turn(
        current_tool_context.get()
    )
    if objective is not None or normalized == "active" or needs_pause_binding:
        if normalized not in {None, "active", "paused"} or reason is not None:
            raise RetryableToolInputError(
                "Edits accept status active or paused without reason; "
                "submit complete or blocked separately"
            )
        ctx = _control_turn()
        try:
            snapshot = await ctx.goal_service.update_from_turn(
                ctx,
                objective=objective,
                resume=normalized == "active",
                pause=normalized == "paused",
            )
        except Exception as exc:
            raise SafeToolError(str(exc)) from exc
        return json.dumps({"status": "accepted", "goal": snapshot}, ensure_ascii=False)
    if normalized is None:
        raise RetryableToolInputError("Provide status or objective")
    service, context = _goal_turn()
    normalized_reason = _optional_text(reason, field="reason", max_chars=1000)
    if normalized == "blocked" and normalized_reason is None:
        raise RetryableToolInputError("reason is required when status is blocked")
    if normalized != "blocked" and normalized_reason is not None:
        raise RetryableToolInputError("reason is only allowed when status is blocked")
    try:
        snapshot = await service.commit_model_status(
            context,
            status=normalized,
            reason=normalized_reason,
        )
    except Exception as exc:  # The service exposes only sanitized contract errors.
        raise SafeToolError(str(exc)) from exc
    return json.dumps(
        {"status": "accepted", "goal": snapshot},
        ensure_ascii=False,
        separators=(",", ":"),
    )


@tool(
    name="update_goal_progress",
    description=(
        "Optionally replace the structured progress view for the Goal owned by this exact "
        "turn. Use it only as a concise view of meaningful multi-step work and keep it "
        "aligned with current reality. Never use it to prescribe fixed phases or future "
        "turns, determine when a turn ends, narrow the objective, pause substantive work, "
        "or substitute for doing the work. Progress does not complete the Goal; call "
        "update_goal separately only when its strict terminal conditions are satisfied."
    ),
    params={
        "explanation": {
            "type": "string",
            "maxLength": 1000,
            "description": (
                "Optional concise explanation of the current state, not a phase or "
                "future-turn instruction."
            ),
        },
        "steps": {
            "type": "array",
            "maxItems": 20,
            "description": (
                "Complete replacement of the optional current-state progress view."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "string", "minLength": 1, "maxLength": 200},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                    },
                },
                "required": ["step", "status"],
                "additionalProperties": False,
            },
        },
    },
    required=["steps"],
    default_access="deny",
    terminates_turn=False,
)
async def update_goal_progress(
    steps: list[dict[str, Any]],
    explanation: str | None = None,
) -> str:
    service, context = _goal_turn()
    normalized_explanation = _optional_text(
        explanation,
        field="explanation",
        max_chars=1000,
    )
    try:
        ctx = current_tool_context.get()
        callback = getattr(ctx, "update_progress", None)
        if ctx is None or not callable(callback):
            raise SafeToolError("Task progress is unavailable in this turn.")
        await callback(steps=steps, explanation=normalized_explanation)
        snapshot = await service.progress_updated(
            context, session_key=ctx.session_key, publish=False,
        )
    except Exception as exc:  # The service exposes only sanitized contract errors.
        raise SafeToolError(str(exc)) from exc
    return json.dumps(
        {"status": "accepted", "goal": snapshot},
        ensure_ascii=False,
        separators=(",", ":"),
    )
