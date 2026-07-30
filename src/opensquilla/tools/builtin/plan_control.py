"""Control tools for Plan collaboration mode and active PlanRun execution."""

from __future__ import annotations

import json
from typing import Any

import structlog

from opensquilla.tools.registry import tool
from opensquilla.tools.types import (
    InteractionMode,
    PlanAccess,
    RetryableToolInputError,
    SafeToolError,
    current_tool_context,
)

_MAX_QUESTION_COUNT = 3
log = structlog.get_logger(__name__)


def _plan_step_status(run: Any, step_id: str | None) -> str:
    """Return the server-authoritative status for one bounded plan step."""

    if not step_id:
        return "unavailable"
    for state in list(getattr(run, "step_states", []) or []):
        if isinstance(state, dict) and str(state.get("step_id") or "") == step_id:
            return str(state.get("status") or "unavailable")
    return "unavailable"


def _checkpoint_conflict_error(
    *,
    requested_step_id: str,
    run: Any | None,
    task_id: str,
) -> SafeToolError:
    """Create a sanitized recovery contract without exposing storage internals."""

    run_status = str(getattr(run, "status", "") or "unavailable")
    current_step_id = str(getattr(run, "current_step_id", "") or "") or None
    current_step_status = _plan_step_status(run, current_step_id)
    same_task = (
        run is not None
        and str(getattr(run, "active_task_id", "") or "") == task_id
    )
    retryable = (
        run_status == "running"
        and current_step_id is not None
        and same_task
    )
    if retryable:
        recovery = {
            "action": "checkpoint_current_step",
            "step_id": current_step_id,
            "allowed_statuses": ["completed", "skipped", "blocked"],
            "instruction": (
                "Retry plan_run_checkpoint for this current step only after it "
                "truthfully reaches the stated result. If later steps are already "
                "finished, checkpoint each missed step one at a time in plan order, "
                "following the current step returned by every successful checkpoint."
            ),
        }
        error: SafeToolError = RetryableToolInputError(
            json.dumps(
                {
                    "error": "plan_checkpoint_conflict",
                    "requested_step_id": requested_step_id,
                    "plan_run_status": run_status,
                    "current_step": {
                        "step_id": current_step_id,
                        "status": current_step_status,
                    },
                    "recovery": recovery,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return error
    return SafeToolError(
        json.dumps(
            {
                "error": "plan_checkpoint_unavailable",
                "requested_step_id": requested_step_id,
                "plan_run_status": run_status,
                "current_step": {
                    "step_id": current_step_id,
                    "status": current_step_status,
                },
                "recovery": {
                    "action": "stop_checkpointing",
                    "instruction": (
                        "Do not retry this checkpoint. The PlanRun is no longer "
                        "owned by this active implementation turn."
                    ),
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _plan_mode_context() -> Any:
    ctx = current_tool_context.get()
    if ctx is None or str(getattr(ctx, "collaboration_mode", "default")) != "plan":
        raise ValueError("This control is available only in Plan mode.")
    if int(getattr(ctx, "subagent_depth", 0) or 0) > 0:
        raise ValueError("Plan submission is unavailable to subagents.")
    return ctx


def _clean_text(value: Any, *, field: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    if len(text) > max_chars:
        raise ValueError(f"{field} must be at most {max_chars} characters")
    return text


def _normalized_steps(steps: Any) -> list[dict[str, Any]]:
    if not isinstance(steps, list):
        raise ValueError("steps must be an array")
    from opensquilla.session.plans import normalize_plan_steps

    # Use the exact durable validator here. A successful terminating control
    # must not fail later because duplicate or non-portable step ids passed a
    # weaker tool-layer check.
    return normalize_plan_steps(steps)


@tool(
    name="submit_plan",
    description=(
        "Submit the complete structured plan for the current Plan turn. "
        "This creates a new immutable revision and ends the turn."
    ),
    params={
        "title": {
            "type": "string",
            "description": "Short plan title.",
        },
        "markdown": {
            "type": "string",
            "description": (
                "Complete human-readable plan. Do not use Markdown task-list "
                "checkboxes as execution state."
            ),
        },
        "steps": {
            "type": "array",
            "description": "Ordered implementation steps for the complete plan.",
            "items": {
                "type": "object",
                "properties": {
                    "step_id": {
                        "type": "string",
                        "description": "Optional stable id; the server creates one if omitted.",
                    },
                    "title": {"type": "string"},
                    "details": {"type": "string"},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
    },
    required=["title", "markdown", "steps"],
    exposed_by_default=False,
    plan_access=PlanAccess.CONTROL,
    terminates_turn=True,
)
async def submit_plan(
    title: str,
    markdown: str,
    steps: list[dict[str, Any]],
) -> str:
    """Validate a plan payload; finalization commits it with the transcript."""

    ctx = _plan_mode_context()
    normalized_title = _clean_text(title, field="title", max_chars=500)
    _clean_text(markdown, field="markdown", max_chars=100_000)
    normalized_steps = _normalized_steps(steps)
    return json.dumps(
        {
            "status": "plan_submitted",
            "title": normalized_title,
            "step_count": len(normalized_steps),
            "parent_revision_id": getattr(ctx, "active_plan_revision_id", None),
            "collaboration_revision": int(
                getattr(ctx, "collaboration_revision", 0) or 0
            ),
        },
        ensure_ascii=False,
    )


@tool(
    name="request_user_input",
    description=(
        "Ask one to three concise questions when a missing user decision "
        "materially changes the plan. On supported interactive surfaces this "
        "waits for the answer and then continues the same Plan turn."
    ),
    params={
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "header": {"type": "string"},
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["label"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "question"],
                "additionalProperties": False,
            },
        }
    },
    required=["questions"],
    exposed_by_default=False,
    plan_access=PlanAccess.CONTROL,
)
async def request_user_input(questions: list[dict[str, Any]]) -> str:
    """Return a structured clarification request without creating a plan."""

    ctx = _plan_mode_context()
    if getattr(ctx, "interaction_mode", None) is not InteractionMode.INTERACTIVE:
        raise ValueError("request_user_input requires an interactive surface")
    if not isinstance(questions, list) or not 1 <= len(questions) <= _MAX_QUESTION_COUNT:
        raise ValueError("questions must contain between one and three items")
    normalized: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(questions):
        if not isinstance(raw, dict):
            raise ValueError(f"questions[{index}] must be an object")
        question_id = _clean_text(
            raw.get("id"),
            field=f"questions[{index}].id",
            max_chars=80,
        )
        if question_id in seen:
            raise ValueError("question ids must be unique")
        seen.add(question_id)
        question_text = _clean_text(
            raw.get("question"),
            field=f"questions[{index}].question",
            max_chars=1000,
        )
        header = str(raw.get("header") or "").strip()
        if header and len(header) > 80:
            raise ValueError(f"questions[{index}].header must be at most 80 characters")
        raw_options = raw.get("options")
        choices: list[str] = []
        normalized_options: list[dict[str, str]] = []
        if raw_options is not None:
            if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= 3:
                raise ValueError(
                    f"questions[{index}].options must contain two or three items"
                )
            for option_index, option in enumerate(raw_options):
                if not isinstance(option, dict):
                    raise ValueError(
                        f"questions[{index}].options[{option_index}] must be an object"
                    )
                label = _clean_text(
                    option.get("label"),
                    field=f"questions[{index}].options[{option_index}].label",
                    max_chars=120,
                )
                description = str(option.get("description") or "").strip()
                if len(description) > 500:
                    raise ValueError(
                        f"questions[{index}].options[{option_index}].description "
                        "must be at most 500 characters"
                    )
                normalized_option = {"label": label}
                if description:
                    normalized_option["description"] = description
                normalized_options.append(normalized_option)
                choices.append(label)
        normalized_question: dict[str, Any] = {
            "id": question_id,
            "question": question_text,
        }
        if header:
            normalized_question["header"] = header
        if normalized_options:
            normalized_question["options"] = normalized_options
        normalized.append(normalized_question)
        field_payload: dict[str, Any] = {
            "name": question_id,
            "prompt": question_text,
            "type": "enum" if choices else "string",
            "required": True,
            "choices": choices,
        }
        if header:
            field_payload["header"] = header
        if normalized_options:
            field_payload["options"] = normalized_options
            # The interactive clients expose a free-form "Other" path in
            # addition to the model-supplied recommendations.
            field_payload["allow_other"] = True
        fields.append(field_payload)
    return json.dumps(
        {
            "status": "input_required",
            "kind": "user_input",
            "paused": True,
            "run_id": str(getattr(ctx, "task_id", "") or ""),
            "step": "plan",
            "clarify_schema": {
                "mode": "form",
                "intro": "The plan needs a decision before it can be completed.",
                "fields": fields,
            },
            "questions": normalized,
        },
        ensure_ascii=False,
    )


@tool(
    name="plan_run_checkpoint",
    description=(
        "Persist progress for the PlanRun attached to this implementation turn. "
        "Checkpoint the current step immediately after it reaches the stated result "
        "and before starting a later step. Never jump over the current step. If work "
        "finished multiple steps before progress was recorded, checkpoint those "
        "steps one at a time in plan order. A blocked checkpoint ends the turn. "
        "After a final completed checkpoint is accepted, publish any final artifact "
        "and write the concise user-facing delivery summary."
    ),
    params={
        "step_id": {
            "type": "string",
            "description": "The plan step whose state changed.",
        },
        "step_status": {
            "type": "string",
            "enum": ["completed", "blocked", "skipped"],
        },
        "next_step_id": {
            "type": "string",
            "description": (
                "Next step to mark in progress. Omit after the final completed "
                "step; the run then completes."
            ),
        },
        "reason": {
            "type": "string",
            "description": "Required explanation when blocked or skipped.",
        },
    },
    required=["step_id", "step_status"],
    exposed_by_default=False,
)
async def plan_run_checkpoint(
    step_id: str,
    step_status: str,
    next_step_id: str | None = None,
    reason: str | None = None,
) -> str:
    """CAS one server-authoritative PlanRun transition and publish its snapshot."""

    ctx = current_tool_context.get()
    if ctx is None:
        raise ValueError("plan_run_checkpoint requires runtime context")
    if str(getattr(ctx, "collaboration_mode", "default")) == "plan":
        raise ValueError("PlanRun progress cannot be changed in Plan mode")
    run_id = str(getattr(ctx, "plan_run_id", "") or "").strip()
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    storage = getattr(ctx, "plan_storage", None)
    if not run_id or not task_id or storage is None:
        raise ValueError(
            "plan_run_checkpoint is available only during plan implementation"
        )
    normalized_step_id = _clean_text(step_id, field="step_id", max_chars=128)
    normalized_status = str(step_status or "").strip().lower()
    if normalized_status not in {"completed", "blocked", "skipped"}:
        raise ValueError("step_status must be completed, blocked, or skipped")
    normalized_next = str(next_step_id or "").strip() or None
    normalized_reason = str(reason or "").strip() or None
    if normalized_status in {"blocked", "skipped"} and normalized_reason is None:
        raise ValueError(f"reason is required when step_status is {normalized_status}")
    if normalized_reason is not None and len(normalized_reason) > 2_000:
        raise ValueError("reason must be at most 2000 characters")

    current = await storage.get_plan_run(run_id)
    if current is None:
        raise ValueError("The active PlanRun no longer exists")
    from opensquilla.session.plans import PlanRunConflictError, plan_run_snapshot

    try:
        updated = await storage.checkpoint_plan_run(
            run_id,
            expected_state_revision=int(current.state_revision),
            step_id=normalized_step_id,
            step_status=normalized_status,
            next_step_id=normalized_next,
            expected_active_task_id=task_id,
            reason=normalized_reason,
        )
    except PlanRunConflictError as exc:
        refreshed = await storage.get_plan_run(run_id)
        raise _checkpoint_conflict_error(
            requested_step_id=normalized_step_id,
            run=refreshed,
            task_id=task_id,
        ) from exc

    snapshot = plan_run_snapshot(updated)
    emitter = getattr(ctx, "plan_event_emitter", None)
    if callable(emitter) and ctx.session_key:
        try:
            await emitter(
                ctx.session_key,
                "session.event.plan_run",
                {"session_key": ctx.session_key, "plan_run": snapshot},
            )
        except Exception as exc:  # noqa: BLE001 - durable checkpoint already committed
            log.warning(
                "plan_run.checkpoint_event_emit_failed",
                session_key=ctx.session_key,
                plan_run_id=run_id,
                state_revision=snapshot["stateRevision"],
                error=str(exc),
            )
    return json.dumps(
        {"status": "checkpoint_recorded", "plan_run": snapshot},
        ensure_ascii=False,
    )
