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


@tool(
    name="update_goal",
    description=(
        "Durably submit the terminal decision for the Goal owned by this exact turn. "
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
        "status": {
            "type": "string",
            "enum": ["complete", "blocked"],
            "description": (
                "The terminal Goal state. Complete requires proof of the full objective; "
                "blocked requires the repeated-blocker and true-impasse conditions."
            ),
        },
        "reason": {
            "type": "string",
            "maxLength": 1000,
            "description": (
                "Required concise description of the repeatedly observed blocker for "
                "blocked; omit for complete."
            ),
        },
    },
    required=["status"],
    exposed_by_default=False,
    terminates_turn=False,
)
async def update_goal(status: str, reason: str | None = None) -> str:
    service, context = _goal_turn()
    normalized = str(status).strip().lower()
    if normalized not in {"complete", "blocked"}:
        raise RetryableToolInputError("status must be complete or blocked")
    normalized_reason = _optional_text(reason, field="reason", max_chars=1000)
    if normalized == "blocked" and normalized_reason is None:
        raise RetryableToolInputError("reason is required when status is blocked")
    if normalized == "complete" and normalized_reason is not None:
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
    exposed_by_default=False,
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
        snapshot = await service.update_progress(
            context,
            explanation=normalized_explanation,
            steps=steps,
        )
    except Exception as exc:  # The service exposes only sanitized contract errors.
        raise SafeToolError(str(exc)) from exc
    return json.dumps(
        {"status": "accepted", "goal": snapshot},
        ensure_ascii=False,
        separators=(",", ":"),
    )
