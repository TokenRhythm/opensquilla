"""Built-in tool for LLM-directed Squilla router control."""

from __future__ import annotations

from opensquilla.router_control import (
    RouterControlHoldStore,
    RouterControlValidationError,
    resolve_router_control_target,
    router_control_rejection_payload,
    router_control_success_payload,
)
from opensquilla.tools.registry import tool
from opensquilla.tools.types import current_tool_context


@tool(
    name="router_control",
    description=(
        "Control the Squilla router for this session. Use only when the user asks "
        "to switch to a configured route or restore automatic routing."
    ),
    params={
        "action": {
            "type": "string",
            "enum": ["set_hold", "clear_hold"],
            "description": "Set a short-lived router hold or clear it.",
        },
        "target_id": {
            "type": "string",
            "description": "Canonical target id from the router-control menu.",
        },
        "evidence": {
            "type": "string",
            "description": "Short excerpt from the user message that requested the switch.",
        },
        "reason": {
            "type": "string",
            "description": "Optional concise reason for observability.",
        },
    },
    required=["action", "evidence"],
)
async def router_control(
    action: str,
    evidence: str,
    target_id: str | None = None,
    reason: str | None = None,  # noqa: ARG001 - reserved for observability hooks
) -> str:
    ctx = current_tool_context.get()
    if ctx is None:
        return router_control_rejection_payload(
            reason="router_control requires runtime tool context",
            evidence=evidence,
        )
    router_cfg = getattr(ctx, "router_control_config", None)
    if router_cfg is None or not getattr(router_cfg, "enabled", False):
        return router_control_rejection_payload(
            reason="squilla router is disabled or unavailable",
            evidence=evidence,
        )
    session_key = getattr(ctx, "session_key", None)
    if not isinstance(session_key, str) or not session_key:
        return router_control_rejection_payload(
            reason="router_control requires a session key",
            evidence=evidence,
        )
    store = getattr(ctx, "router_control_hold_store", None)
    if not isinstance(store, RouterControlHoldStore):
        return router_control_rejection_payload(
            reason="router_control hold store is unavailable",
            evidence=evidence,
        )

    normalized_action = str(action or "").strip()
    replay_depth = int(getattr(ctx, "router_control_replay_depth", 0) or 0)
    if normalized_action == "clear_hold":
        store.clear(session_key)
        replay_required = bool(
            getattr(ctx, "router_control_turn_hold_applied", False)
            and replay_depth <= 0
        )
        return router_control_success_payload(
            action="clear_hold",
            target=None,
            replay_required=replay_required,
            evidence=evidence,
        )

    if normalized_action != "set_hold":
        return router_control_rejection_payload(
            reason=f"unsupported router_control action {normalized_action!r}",
            evidence=evidence,
        )
    if not str(evidence or "").strip():
        return router_control_rejection_payload(
            reason="set_hold requires evidence",
            evidence=evidence,
        )
    try:
        target = resolve_router_control_target(router_cfg, str(target_id or ""))
    except RouterControlValidationError as exc:
        return router_control_rejection_payload(reason=str(exc), evidence=evidence)

    store.set_hold(session_key, target, evidence=evidence)
    return router_control_success_payload(
        action="set_hold",
        target=target,
        replay_required=replay_depth <= 0,
        evidence=evidence,
    )
