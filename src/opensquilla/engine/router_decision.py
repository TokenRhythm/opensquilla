"""Router decision event construction."""

from __future__ import annotations

from typing import Any, cast

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.route_plan import RoutePlan
from opensquilla.engine.types import RouterDecisionEvent
from opensquilla.provider.model_catalog import shared_catalog
from opensquilla.router_tiers import normalize_text_tier, tier_index


def _resolve_context_window(model_id: str) -> int | None:
    if not model_id:
        return None
    return shared_catalog().resolve_context_window(model_id)


def _coerce_probs(value: object) -> list[float]:
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: str(item[0]))
        raw_values = [item_value for _item_key, item_value in items]
    elif isinstance(value, list | tuple):
        raw_values = list(value)
    else:
        raw_values = []

    probs: list[float] = []
    for raw_value in raw_values[:4]:
        try:
            probs.append(float(raw_value))
        except (TypeError, ValueError):
            probs.append(0.0)
    return probs


def _coerce_float(value: object, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return default


def build_router_decision_event(turn: TurnContext) -> RouterDecisionEvent | None:
    """Construct a RouterDecisionEvent from post-pipeline turn metadata."""

    plan = getattr(turn, "route_plan", None)
    routed_tier = plan.tier if isinstance(plan, RoutePlan) else turn.metadata.get("routed_tier")
    if not routed_tier:
        return None

    extra = turn.metadata.get("routing_extra")
    if not isinstance(extra, dict):
        extra = {}

    probs = _coerce_probs(extra.get("probabilities", extra.get("probs")))

    tier_savings = extra.get("tier_savings")
    savings_pct = _coerce_float(turn.metadata.get("savings_pct"))
    if isinstance(tier_savings, dict):
        savings_pct = savings_pct or _coerce_float(tier_savings.get("pct"))

    routed_tier = normalize_text_tier(routed_tier) or routed_tier
    tier_idx = tier_index(routed_tier)

    if isinstance(plan, RoutePlan):
        source = plan.source
        routing_applied = plan.routing_applied
        model = plan.model
        thinking_mode = plan.thinking
        prompt_policy = plan.prompt_policy
        context_window = plan.capabilities.context_window or None
    else:
        source = str(turn.metadata.get("routing_source") or "none")
        raw_routing_applied = turn.metadata.get("routing_applied")
        routing_applied = (
            True if raw_routing_applied is None else bool(raw_routing_applied)
        )
        model = str(turn.metadata.get("routed_model") or turn.model or "")
        thinking_mode = str(turn.metadata.get("thinking_mode") or "")
        prompt_policy = str(turn.metadata.get("prompt_policy") or "")
        context_window = _resolve_context_window(model)

    return RouterDecisionEvent(
        tier=str(routed_tier),
        tier_index=tier_idx,
        model=model,
        baseline_model=str(turn.metadata.get("baseline_model") or ""),
        source=source,
        confidence=float(turn.metadata.get("routing_confidence") or 0.0),
        probs=probs,
        savings_pct=savings_pct,
        fallback=source == "fallback",
        thinking_mode=thinking_mode,
        prompt_policy=prompt_policy,
        routing_applied=bool(routing_applied),
        rollout_phase=str(turn.metadata.get("rollout_phase") or "full"),
        context_window=context_window,
    )
