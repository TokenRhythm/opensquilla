"""Gateway adapter for child selection through the Squilla tree-model router."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.steps.squilla_router import apply_squilla_router

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ChildTreeRoute:
    model: str
    provider: str | None = None
    tier: str | None = None
    source: str | None = None
    confidence: float | None = None
    thinking_level: str | None = None


async def select_child_tree_route(
    task: str,
    *,
    session_key: str,
    baseline_model: str,
    config: object | None,
) -> ChildTreeRoute | None:
    """Invoke ``apply_squilla_router(TurnContext)`` once for a new child.

    This is an adapter around the existing router entrypoint, not a second
    routing contract.  Its opt-in metadata matches the established subagent
    selection path, while later child turns remain pinned by the router's
    existing child-turn guard.
    """

    router_config = getattr(config, "squilla_router", None) if config is not None else None
    if router_config is None or not getattr(router_config, "enabled", False):
        return None

    context = TurnContext(
        message=task,
        raw_message=task,
        routing_hint=task,
        session_key=session_key,
        config=config,
        provider=None,
        model=baseline_model,
        tool_defs=[],
        system_prompt="",
        metadata={
            "subagent_spawn_routing": True,
            "_defer_squilla_router_history": True,
        },
    )
    try:
        routed = await apply_squilla_router(context)
    except Exception as exc:  # noqa: BLE001 - existing router behavior is fail-open
        log.warning(
            "orchestration.child_model_route_failed",
            session_key=session_key,
            error=str(exc),
        )
        return None

    routed_model = str(routed.metadata.get("routed_model") or "").strip()
    if not routed.metadata.get("routing_applied") or not routed_model:
        return None
    raw_confidence: Any = routed.metadata.get("routing_confidence")
    try:
        confidence = float(raw_confidence) if raw_confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    return ChildTreeRoute(
        model=routed_model,
        provider=str(routed.metadata.get("routed_provider") or "").strip() or None,
        tier=str(routed.metadata.get("routed_tier") or "").strip() or None,
        source=str(routed.metadata.get("routing_source") or "").strip() or None,
        confidence=confidence,
        thinking_level=str(routed.metadata.get("thinking_level") or "").strip() or None,
    )


__all__ = ["ChildTreeRoute", "select_child_tree_route"]
