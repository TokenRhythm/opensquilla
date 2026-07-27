"""Registration of the ``update_plan`` planning tool.

``update_plan`` lets the model maintain a short task plan across a long
multi-step task. Each call replaces the whole plan; the latest plan is kept
in a session-scoped store so later engine code (or tests) can inspect it.

Visibility: ``exposed_by_default=False``. The tool never appears on the
default surface; deployments opt in via ``[tools] also_allow`` under a named
profile, or by adding ``update_plan`` to ``ToolContext.allowed_tools`` /
``ToolContext.surfaced_tools``.

Dispatch: like every other hidden builtin (meta-skill tools, memory tools,
nodes), ``exposed_by_default=False`` controls listing only — a caller that
already knows the name can dispatch it, subject to the normal policy chain
(channel callers are still denied by the channel profile). Likewise,
``allow = ["*"]`` expands over hidden tools by definition and will surface
this one too. Both follow the engine-wide contract; restricting them here
would fork hidden-tool semantics for a single tool.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from opensquilla.tools.registry import tool
from opensquilla.tools.types import ToolError, current_tool_context

_VALID_STATUSES = ("pending", "in_progress", "completed")
_STATUS_MARKERS = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
_MAX_STEPS = 64
_MAX_STEP_CHARS = 512

# ToolContext exposes no per-session KV surface for tools, so the latest plan
# lives in a module-level store keyed by session key. FIFO eviction bounds
# memory in long-lived processes; entries die with the process — there is no
# persistence beyond the session.
_PLAN_STORE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_PLAN_STORE_MAX_SESSIONS = 128


def latest_plan(session_key: str) -> dict[str, Any] | None:
    """Return the stored plan entry for *session_key* (treat as read-only)."""

    return _PLAN_STORE.get(session_key)


def _validated_steps(plan: Any) -> list[dict[str, str]]:
    if not isinstance(plan, list) or not plan:
        raise ToolError("plan must be a non-empty array of steps")
    if len(plan) > _MAX_STEPS:
        raise ToolError(f"plan supports at most {_MAX_STEPS} steps, got {len(plan)}")
    steps: list[dict[str, str]] = []
    for index, item in enumerate(plan):
        if not isinstance(item, Mapping):
            raise ToolError(f"plan[{index}] must be an object with step and status")
        step = item.get("step")
        status = item.get("status")
        if not isinstance(step, str) or not step.strip():
            raise ToolError(f"plan[{index}].step must be a non-empty string")
        if len(step) > _MAX_STEP_CHARS:
            raise ToolError(
                f"plan[{index}].step must be at most {_MAX_STEP_CHARS} characters"
            )
        if status not in _VALID_STATUSES:
            raise ToolError(
                f"plan[{index}].status must be one of {list(_VALID_STATUSES)!r}"
            )
        # Extra keys on a step are tolerated (schema validation does not set
        # additionalProperties); only step and status are kept.
        steps.append({"step": step, "status": status})
    return steps


def _store_plan(
    ctx: Any,
    steps: list[dict[str, str]],
    explanation: str | None,
) -> None:
    if ctx is None:
        return
    key = getattr(ctx, "session_key", None) or getattr(ctx, "agent_id", None)
    if not key:
        return
    previous = _PLAN_STORE.pop(key, None)
    _PLAN_STORE[key] = {
        "steps": steps,
        "explanation": explanation,
        "revision": (previous or {}).get("revision", 0) + 1,
    }
    while len(_PLAN_STORE) > _PLAN_STORE_MAX_SESSIONS:
        _PLAN_STORE.popitem(last=False)


def _emit_plan_updated_event(ctx: Any, counts: dict[str, int], step_count: int) -> None:
    callback = getattr(ctx, "on_runtime_event", None) if ctx is not None else None
    if callback is None:
        return
    event = {
        "feature": "update_plan",
        "name": "update_plan.updated",
        "action": "replace_plan",
        "tool": "update_plan",
        "tool_name": "update_plan",
        "step_count": step_count,
        "completed": counts["completed"],
        "in_progress": counts["in_progress"],
        "pending": counts["pending"],
        "agent_id": getattr(ctx, "agent_id", None),
        "session_key": getattr(ctx, "session_key", None),
    }
    try:
        callback(event)
    except Exception:
        return


@tool(
    name="update_plan",
    description=(
        "Maintain a concise plan for the current task and keep it current as "
        "work progresses. Each call replaces the entire plan, so always send "
        "the full list of steps rather than a delta. Write short, "
        "outcome-oriented steps. Keep exactly one step in_progress while work "
        "is underway, mark steps completed as soon as they are done, and call "
        "this tool again whenever statuses or steps change. Use it for any "
        "long multi-step task."
    ),
    params={
        "plan": {
            "type": "array",
            "description": "The full updated plan. Always send every step, not a delta.",
            "items": {
                "type": "object",
                "properties": {
                    "step": {
                        "type": "string",
                        "description": "Short, outcome-oriented step description.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed"],
                        "description": "Current status of the step.",
                    },
                },
                "required": ["step", "status"],
            },
        },
        "explanation": {
            "type": "string",
            "description": "Optional short note about why the plan changed.",
        },
    },
    required=["plan"],
    exposed_by_default=False,
)
async def update_plan(
    plan: list[dict[str, Any]],
    explanation: str | None = None,
) -> str:
    # Dispatch schema validation covers registered calls; re-validate here so
    # direct handler calls get the same ToolError contract.
    steps = _validated_steps(plan)
    counts = {status: 0 for status in _VALID_STATUSES}
    for item in steps:
        counts[item["status"]] += 1

    ctx = current_tool_context.get()
    _store_plan(ctx, steps, explanation)
    _emit_plan_updated_event(ctx, counts, len(steps))

    step_word = "step" if len(steps) == 1 else "steps"
    lines = [
        f"Plan updated ({len(steps)} {step_word}: "
        f"{counts['completed']} completed, {counts['in_progress']} in_progress, "
        f"{counts['pending']} pending)"
    ]
    lines.extend(f"{_STATUS_MARKERS[item['status']]} {item['step']}" for item in steps)
    if counts["in_progress"] > 1:
        lines.append(
            f"warning: {counts['in_progress']} steps are in_progress; "
            "keep at most one step in_progress."
        )
    return "\n".join(lines)
