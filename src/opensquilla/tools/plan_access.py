"""Fail-closed tool access checks for Plan collaboration mode."""

from __future__ import annotations

import json

from opensquilla.execution_status import normalize_execution_status
from opensquilla.tool_boundary import ToolCall, ToolResult
from opensquilla.tools.envelope import build_tool_failure_envelope
from opensquilla.tools.types import PlanAccess, RegisteredTool, ToolContext, ToolSpec


def is_plan_mode(ctx: ToolContext | None) -> bool:
    """Return whether the turn's frozen collaboration mode is Plan."""

    if ctx is None:
        return False
    mode = getattr(ctx, "collaboration_mode", "default")
    return getattr(mode, "value", mode) == "plan"


def plan_access_allows(spec: ToolSpec, ctx: ToolContext | None) -> bool:
    """Return whether *spec* passes the Plan-only capability boundary.

    Invalid or foreign ``plan_access`` values are denied in Plan mode. Outside
    Plan mode this policy is intentionally inert to preserve existing callers.
    """

    if not is_plan_mode(ctx):
        return True
    try:
        access = PlanAccess(getattr(spec, "plan_access", PlanAccess.DENY))
    except (TypeError, ValueError):
        access = PlanAccess.DENY
    return access is not PlanAccess.DENY


def preflight_plan_access(
    tool_call: ToolCall,
    registered: RegisteredTool,
    ctx: ToolContext | None,
) -> ToolResult | None:
    """Return an authoritative denial before any validation or side effect."""

    if plan_access_allows(registered.spec, ctx):
        return None
    user_message = (
        f"Tool '{tool_call.tool_name}' is unavailable in Plan mode. "
        "Continue with read-only planning tools."
    )
    status = {
        "version": 1,
        "status": "error",
        "exit_code": None,
        "timed_out": False,
        "truncated": False,
        "reason": "plan_mode_denied",
        "source": "tool_runtime",
        "preservation_class": "diagnostic",
        "preflight_rejected": True,
        "reason_code": "plan_mode_denied",
    }
    return ToolResult(
        tool_use_id=tool_call.tool_use_id,
        tool_name=tool_call.tool_name,
        content=json.dumps(
            build_tool_failure_envelope(
                PermissionError("tool denied in Plan mode"),
                tool_call.tool_name,
                policy_denial=True,
                error_class_override="PolicyDenied",
                user_message_override=user_message,
            )
        ),
        is_error=True,
        execution_status=normalize_execution_status(status),
    )
