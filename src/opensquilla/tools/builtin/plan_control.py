"""Control tools for Plan collaboration mode and active PlanRun execution."""

from __future__ import annotations

import json
from typing import Any

import structlog

from opensquilla.session.plans import (
    MAX_PLAN_MARKDOWN_CHARS,
    MAX_PLAN_STEP_DETAILS_CHARS,
    MAX_PLAN_STEP_ID_CHARS,
    MAX_PLAN_STEP_REASON_CHARS,
    MAX_PLAN_STEP_TITLE_CHARS,
    MAX_PLAN_STEPS,
    MAX_PLAN_TITLE_CHARS,
)
from opensquilla.tools.registry import tool
from opensquilla.tools.types import (
    InteractionMode,
    PlanAccess,
    RetryableToolInputError,
    SafeToolError,
    current_tool_context,
)

_MAX_QUESTION_COUNT = 3
_MAX_QUESTION_ID_CHARS = 80
_MAX_QUESTION_HEADER_CHARS = 80
_MAX_QUESTION_TEXT_CHARS = 1_000
_MIN_OPTION_COUNT = 2
_MAX_OPTION_COUNT = 3
_MAX_OPTION_LABEL_CHARS = 120
_MAX_OPTION_DESCRIPTION_CHARS = 500
log = structlog.get_logger(__name__)


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
        raise RetryableToolInputError(f"{field} is required")
    if len(text) > max_chars:
        raise RetryableToolInputError(
            f"{field} must be at most {max_chars} characters"
        )
    return text


def _normalized_steps(steps: Any) -> list[dict[str, Any]]:
    if not isinstance(steps, list):
        raise RetryableToolInputError("steps must be an array")
    from opensquilla.session.plans import PlanValidationError, normalize_plan_steps

    # Use the exact durable validator here. A successful terminating control
    # must not fail later because duplicate or non-portable step ids passed a
    # weaker tool-layer check.
    try:
        return normalize_plan_steps(steps)
    except PlanValidationError as exc:
        raise RetryableToolInputError(str(exc)) from exc


@tool(
    name="submit_plan",
    description=(
        "Submit the complete structured plan for user review in the current Plan turn. "
        "This creates a new immutable revision and ends the planning turn; "
        "it does not authorize or start implementation."
    ),
    params={
        "title": {
            "type": "string",
            "description": "Short plan title.",
            "minLength": 1,
            "maxLength": MAX_PLAN_TITLE_CHARS,
        },
        "markdown": {
            "type": "string",
            "description": (
                "Complete human-readable plan. Do not use Markdown task-list "
                "checkboxes as execution state."
            ),
            "minLength": 1,
            "maxLength": MAX_PLAN_MARKDOWN_CHARS,
        },
        "steps": {
            "type": "array",
            "description": "Ordered implementation steps for the complete plan.",
            "minItems": 1,
            "maxItems": MAX_PLAN_STEPS,
            "items": {
                "type": "object",
                "properties": {
                    "step_id": {
                        "type": "string",
                        "description": "Optional stable id; the server creates one if omitted.",
                        "minLength": 1,
                        "maxLength": MAX_PLAN_STEP_ID_CHARS,
                    },
                    "title": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_PLAN_STEP_TITLE_CHARS,
                    },
                    "details": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_PLAN_STEP_DETAILS_CHARS,
                    },
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
    },
    required=["title", "markdown", "steps"],
    default_access="deny",
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
    normalized_title = _clean_text(
        title,
        field="title",
        max_chars=MAX_PLAN_TITLE_CHARS,
    )
    _clean_text(
        markdown,
        field="markdown",
        max_chars=MAX_PLAN_MARKDOWN_CHARS,
    )
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
        "materially changes the task. On supported interactive surfaces this "
        "waits for the answer and then continues the same task."
    ),
    params={
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": _MAX_QUESTION_COUNT,
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _MAX_QUESTION_ID_CHARS,
                    },
                    "header": {
                        "type": "string",
                        "maxLength": _MAX_QUESTION_HEADER_CHARS,
                    },
                    "question": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _MAX_QUESTION_TEXT_CHARS,
                    },
                    "options": {
                        "type": "array",
                        "minItems": _MIN_OPTION_COUNT,
                        "maxItems": _MAX_OPTION_COUNT,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": _MAX_OPTION_LABEL_CHARS,
                                },
                                "description": {
                                    "type": "string",
                                    "maxLength": _MAX_OPTION_DESCRIPTION_CHARS,
                                },
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
    default_access="deny",
    plan_access=PlanAccess.CONTROL,
)
async def request_user_input(questions: list[dict[str, Any]]) -> str:
    """Return a structured clarification request without creating a plan."""

    ctx = current_tool_context.get()
    if ctx is None:
        raise ValueError("request_user_input requires runtime context")
    if getattr(ctx, "interaction_mode", None) is not InteractionMode.INTERACTIVE:
        raise ValueError("request_user_input requires an interactive surface")
    if not isinstance(questions, list) or not 1 <= len(questions) <= _MAX_QUESTION_COUNT:
        raise RetryableToolInputError(
            "questions must contain between one and three items"
        )
    normalized: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(questions):
        if not isinstance(raw, dict):
            raise RetryableToolInputError(f"questions[{index}] must be an object")
        question_id = _clean_text(
            raw.get("id"),
            field=f"questions[{index}].id",
            max_chars=_MAX_QUESTION_ID_CHARS,
        )
        if question_id in seen:
            raise RetryableToolInputError("question ids must be unique")
        seen.add(question_id)
        question_text = _clean_text(
            raw.get("question"),
            field=f"questions[{index}].question",
            max_chars=_MAX_QUESTION_TEXT_CHARS,
        )
        header = str(raw.get("header") or "").strip()
        if header and len(header) > _MAX_QUESTION_HEADER_CHARS:
            raise RetryableToolInputError(
                f"questions[{index}].header must be at most "
                f"{_MAX_QUESTION_HEADER_CHARS} characters"
            )
        raw_options = raw.get("options")
        choices: list[str] = []
        normalized_options: list[dict[str, str]] = []
        if raw_options is not None:
            if (
                not isinstance(raw_options, list)
                or not _MIN_OPTION_COUNT <= len(raw_options) <= _MAX_OPTION_COUNT
            ):
                raise RetryableToolInputError(
                    f"questions[{index}].options must contain two or three items"
                )
            seen_labels: set[str] = set()
            for option_index, option in enumerate(raw_options):
                if not isinstance(option, dict):
                    raise RetryableToolInputError(
                        f"questions[{index}].options[{option_index}] must be an object"
                    )
                label = _clean_text(
                    option.get("label"),
                    field=f"questions[{index}].options[{option_index}].label",
                    max_chars=_MAX_OPTION_LABEL_CHARS,
                )
                if label in seen_labels:
                    raise RetryableToolInputError(
                        f"questions[{index}].option labels must be unique"
                    )
                seen_labels.add(label)
                description = str(option.get("description") or "").strip()
                if len(description) > _MAX_OPTION_DESCRIPTION_CHARS:
                    raise RetryableToolInputError(
                        f"questions[{index}].options[{option_index}].description "
                        f"must be at most {_MAX_OPTION_DESCRIPTION_CHARS} characters"
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
                "presentation": "plan_questionnaire_v1",
                "intro": "The task needs a decision before it can be completed.",
                "fields": fields,
            },
            "questions": normalized,
        },
        ensure_ascii=False,
    )


@tool(
    name="update_plan",
    description=(
        "Replace the optional progress list for this task. Use only when a concise "
        "progress view helps with substantive multi-step work. Skip simple questions "
        "and single-step tasks. Update only when steps or their status materially "
        "change; batch related changes instead of updating after every tool call, "
        "and do not resend an unchanged list. Add, remove, reorder or reopen steps "
        "as the work changes. This does not enter Plan mode or create a Goal. "
        "Progress describes actual work and does not control tool permissions, "
        "execution order or task completion."
    ),
    params={
        "steps": {
            "type": "array", "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "string", "minLength": 1, "maxLength": 200},
                    "status": {"type": "string",
                               "enum": ["pending", "in_progress", "completed"]},
                },
                "required": ["step", "status"], "additionalProperties": False,
            },
        },
        "explanation": {"type": "string", "maxLength": 1000},
    },
    required=["steps"],
    default_access="deny",
)
async def update_plan(steps: list[dict[str, Any]], explanation: str | None = None) -> str:
    ctx = current_tool_context.get()
    if (
        ctx is None or ctx.subagent_depth > 0
        or str(ctx.collaboration_mode) != "default"
        or not callable(ctx.update_progress)
    ):
        raise SafeToolError("Progress requires a running main Default task")
    progress = await ctx.update_progress(steps, explanation)
    return json.dumps({"status": "accepted", "progress": progress}, ensure_ascii=False)


@tool(
    name="plan_run_checkpoint",
    description=(
        "Compatibility progress update for a previously proposed step. "
        "Prefer update_plan for a complete, adjustable progress list. "
        "This does not stop the task or constrain subsequent tools."
    ),
    params={
        "step_id": {"type": "string", "maxLength": MAX_PLAN_STEP_ID_CHARS},
        "step_status": {"type": "string", "enum": ["completed", "blocked", "skipped"]},
        "reason": {"type": "string", "maxLength": MAX_PLAN_STEP_REASON_CHARS},
    },
    required=["step_id", "step_status"],
    default_access="deny",
)
async def plan_run_checkpoint(
    step_id: str, step_status: str, next_step_id: str | None = None,
    reason: str | None = None,
) -> str:
    """Translate an old checkpoint into the shared task progress update."""
    ctx = current_tool_context.get()
    if ctx is None or not ctx.plan_run_id or ctx.plan_storage is None:
        raise SafeToolError("Checkpoint requires a current plan implementation")
    if step_status not in {"completed", "blocked", "skipped"}:
        raise RetryableToolInputError("Invalid checkpoint status")
    from opensquilla.session.plans import PlanValidationError, checkpoint_plan_progress

    proposed = list(getattr(ctx.plan_revision, "steps", []) or [])
    task = await ctx.plan_storage.get_agent_task(ctx.task_id)
    metadata = ((task.details or {}).get("metadata") or {}) if task else {}
    run = await ctx.plan_storage.get_plan_run(ctx.plan_run_id)
    if (
        run is None or run.status != "running" or run.active_task_id != ctx.task_id
        or metadata.get("plan_run_id") != ctx.plan_run_id
    ):
        raise SafeToolError("Checkpoint requires the current task's attached plan run")
    prior = metadata.get("progress") or {}
    steps = prior.get("steps")
    if steps is None:
        source = getattr(run, "step_states", None) or proposed
        steps = [
            {"step": item["title"], "status": (
                item["status"] if item.get("status") in {"completed", "in_progress"}
                else "pending"
            )}
            for item in source
        ]
    try:
        steps = checkpoint_plan_progress(
            proposed, steps, step_id=step_id, step_status=step_status,
            next_step_id=next_step_id, reason=reason,
        )
    except PlanValidationError as exc:
        raise RetryableToolInputError(str(exc)) from exc
    response = json.loads(await update_plan(steps, reason))
    from opensquilla.session.plans import plan_run_snapshot

    run = await ctx.plan_storage.get_plan_run(ctx.plan_run_id)
    response.update(status="checkpoint_recorded", plan_run=plan_run_snapshot(run))
    return json.dumps(response, ensure_ascii=False)
