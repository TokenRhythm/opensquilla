"""Router-selected soft and hard iteration budgets for subagents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_ROUTE_TO_EFFORT = {
    "c0": "small",
    "c1": "small",
    "c2": "medium",
    "c3": "large",
    "image": "large",
}


@dataclass(frozen=True)
class SubagentIterationBudget:
    effort_tier: str
    soft_limit: int
    hard_limit: int
    route_tier: str | None


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def resolve_subagent_iteration_budget(
    *,
    route_tier: str | None,
    subagents_config: Any,
) -> SubagentIterationBudget:
    """Resolve one child budget from the model router's existing tier result."""

    config = _value(subagents_config, "iteration_budget")
    fallback_tier = str(_value(config, "fallback_tier", "medium") or "medium")
    effort_tier = _ROUTE_TO_EFFORT.get(str(route_tier or "").lower(), fallback_tier)
    if effort_tier not in {"small", "medium", "large"}:
        effort_tier = "medium"

    defaults = {
        "small": (6, 10),
        "medium": (12, 20),
        "large": (24, 36),
    }
    tier_config = _value(config, effort_tier)
    default_soft, default_hard = defaults[effort_tier]
    soft_limit = int(_value(tier_config, "soft", default_soft) or default_soft)
    hard_limit = int(_value(tier_config, "hard", default_hard) or default_hard)
    return SubagentIterationBudget(
        effort_tier=effort_tier,
        soft_limit=soft_limit,
        hard_limit=hard_limit,
        route_tier=route_tier,
    )


__all__ = ["SubagentIterationBudget", "resolve_subagent_iteration_budget"]
