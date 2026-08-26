"""Immutable logical routing plan and append-only execution-leg telemetry.

The router decides once, before the agent loop starts.  Provider retries and
selector failover are physical execution details of that decision; they must
not be represented as additional router decisions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from opensquilla.provider.types import ModelCapabilities, ProviderRequestCorrelation
from opensquilla.router_tiers import (
    IMAGE_TIER,
    TEXT_TIERS,
    TierConfig,
    effective_ensemble_selection_mode,
    normalize_tier_id,
    normalize_tier_mapping,
    tier_ensemble_active,
    tier_ensemble_execution,
)

_ROUTER_SNAPSHOT_TIER_ORDER = (*TEXT_TIERS, IMAGE_TIER)


@dataclass(frozen=True, slots=True)
class RouteFallback:
    """One configured fallback candidate captured when the route is pinned."""

    tier: str
    provider: str
    model: str
    capabilities: RouteCapabilitySnapshot

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "provider": self.provider,
            "model": self.model,
            "capabilities": self.capabilities.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class RouteCapabilitySnapshot:
    """Capacity and feature facts used by this logical turn."""

    context_window: int
    supports_reasoning: bool | None
    supports_tools: bool | None
    supports_streaming: bool | None
    supports_vision: bool | None
    reasoning_format: str
    # A provider/model-specific automatic output ceiling.  Zero means the
    # catalog did not have an authoritative value and physical fallback must
    # preserve the caller's request unchanged.
    effective_max_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "context_window": self.context_window,
            "effective_max_tokens": self.effective_max_tokens,
            "supports_reasoning": self.supports_reasoning,
            "supports_tools": self.supports_tools,
            "supports_streaming": self.supports_streaming,
            "supports_vision": self.supports_vision,
            "reasoning_format": self.reasoning_format,
        }


@dataclass(frozen=True, slots=True)
class RouterTierSnapshotEntry:
    """One display-safe candidate from the accepted routing configuration."""

    tier: str
    provider: str
    model: str
    execution_kind: Literal["single_model", "ensemble"]

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "tier": self.tier,
            "model": self.model,
            "execution_kind": self.execution_kind,
        }
        if self.provider:
            payload["provider"] = self.provider
        return payload


@dataclass(frozen=True, slots=True)
class RouterTierSnapshot:
    """Versioned candidate pool frozen for one routed request."""

    version: Literal[1]
    request_kind: Literal["text", "image"]
    tiers: tuple[RouterTierSnapshotEntry, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "request_kind": self.request_kind,
            "tiers": [item.as_dict() for item in self.tiers],
        }


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """One immutable router decision for one logical turn."""

    version: int
    plan_id: str
    turn_id: str
    tier: str
    provider: str
    model: str
    source: str
    routing_applied: bool
    thinking: str
    prompt_policy: str
    fallback_chain: tuple[RouteFallback, ...]
    capabilities: RouteCapabilitySnapshot
    router_tier_snapshot: RouterTierSnapshot | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "version": self.version,
            "plan_id": self.plan_id,
            "turn_id": self.turn_id,
            "tier": self.tier,
            "provider": self.provider,
            "model": self.model,
            "source": self.source,
            "routing_applied": self.routing_applied,
            "thinking": self.thinking,
            "prompt_policy": self.prompt_policy,
            "fallback_chain": [item.as_dict() for item in self.fallback_chain],
            "capabilities": self.capabilities.as_dict(),
        }
        if self.router_tier_snapshot is not None:
            payload["router_tier_snapshot"] = self.router_tier_snapshot.as_dict()
        return payload


def _text(value: object) -> str:
    return str(value or "").strip()


def _fallback_chain(
    value: object,
    *,
    default_provider: str,
    primary_model: str,
    capability_snapshots: Mapping[
        tuple[str, str],
        tuple[int, ModelCapabilities | None]
        | tuple[int, int, ModelCapabilities | None],
    ] | None,
) -> tuple[RouteFallback, ...]:
    if not isinstance(value, list):
        return ()
    result: list[RouteFallback] = []
    seen: set[tuple[str, str]] = {(default_provider, primary_model)}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        model = _text(item.get("model"))
        if not model:
            continue
        provider = _text(item.get("provider")) or default_provider
        identity = (provider, model)
        if identity in seen:
            continue
        seen.add(identity)
        raw_snapshot = (capability_snapshots or {}).get(identity, (0, None))
        if len(raw_snapshot) == 3:
            context_window, effective_max_tokens, capabilities = raw_snapshot
        else:
            context_window, capabilities = raw_snapshot
            effective_max_tokens = 0
        result.append(
            RouteFallback(
                tier=_text(item.get("tier")),
                provider=provider,
                model=model,
                capabilities=_capability_snapshot(
                    context_window=context_window,
                    effective_max_tokens=effective_max_tokens,
                    capabilities=capabilities,
                ),
            )
        )
    return tuple(result)


def _capability_snapshot(
    *,
    context_window: int,
    effective_max_tokens: int = 0,
    capabilities: ModelCapabilities | None,
) -> RouteCapabilitySnapshot:
    return RouteCapabilitySnapshot(
        context_window=max(0, int(context_window or 0)),
        effective_max_tokens=max(0, int(effective_max_tokens or 0)),
        supports_reasoning=(
            bool(capabilities.supports_reasoning)
            if capabilities is not None
            else None
        ),
        supports_tools=(
            capabilities.supports_tools
            if capabilities is not None
            and isinstance(capabilities.supports_tools, bool)
            else None
        ),
        supports_streaming=(
            bool(capabilities.supports_streaming)
            if capabilities is not None
            else None
        ),
        supports_vision=(
            bool(capabilities.supports_vision)
            if capabilities is not None
            else None
        ),
        reasoning_format=(
            _text(capabilities.reasoning_format)
            if capabilities is not None
            else ""
        ),
    )


def _thinking_snapshot(metadata: Mapping[str, Any], effective_thinking: object) -> str:
    explicit = _text(metadata.get("thinking_level") or metadata.get("thinking_mode"))
    if explicit:
        return explicit
    value = getattr(effective_thinking, "value", effective_thinking)
    if isinstance(value, bool):
        return "enabled" if value else "disabled"
    return _text(value)


def _router_tier_snapshot(
    config: Any,
    metadata: Mapping[str, Any],
    *,
    winner_tier: object,
    winner_provider: str,
    winner_model: str,
) -> RouterTierSnapshot | None:
    """Freeze the candidate pool that was accepted for this logical turn."""

    router = getattr(config, "squilla_router", None)
    tiers = normalize_tier_mapping(getattr(router, "tiers", None))
    normalized_winner = normalize_tier_id(winner_tier)
    if not tiers or normalized_winner is None or not winner_model:
        return None

    request_kind: Literal["text", "image"] = (
        "image"
        if _text(metadata.get("routing_source")) == "image_route"
        or bool(metadata.get("image_route_reason"))
        or bool(metadata.get("router_vision_followup_needs_image"))
        or normalized_winner == IMAGE_TIER
        else "text"
    )
    shared_selection_mode = effective_ensemble_selection_mode(config)
    ensemble = getattr(config, "llm_ensemble", None)
    c3_fusion_active = bool(getattr(ensemble, "enabled", False)) or tier_ensemble_active(
        tiers,
        TEXT_TIERS[-1],
    )

    entries: list[RouterTierSnapshotEntry] = []
    for tier in _ROUTER_SNAPSHOT_TIER_ORDER:
        tier_config = TierConfig.from_value(tiers.get(tier))
        if not tier_config.model:
            continue
        if request_kind == "image":
            if not tier_config.supports_image:
                continue
            if tier == TEXT_TIERS[-1] and c3_fusion_active:
                continue
        elif tier_config.image_only or tier == IMAGE_TIER:
            continue

        selection_mode, _binding = tier_ensemble_execution(
            tiers,
            tier,
            shared_selection_mode=shared_selection_mode,
        )
        entries.append(
            RouterTierSnapshotEntry(
                tier=tier,
                provider=tier_config.provider,
                model=tier_config.model,
                execution_kind="ensemble" if selection_mode else "single_model",
            )
        )

    winner_index = next(
        (index for index, item in enumerate(entries) if item.tier == normalized_winner),
        None,
    )
    winner_entry = RouterTierSnapshotEntry(
        tier=normalized_winner,
        provider=winner_provider,
        model=winner_model,
        execution_kind=(
            entries[winner_index].execution_kind
            if winner_index is not None
            else "single_model"
        ),
    )
    if winner_index is None:
        entries.append(winner_entry)
        entries.sort(key=lambda item: _ROUTER_SNAPSHOT_TIER_ORDER.index(item.tier))
    else:
        entries[winner_index] = winner_entry

    return RouterTierSnapshot(version=1, request_kind=request_kind, tiers=tuple(entries))


def pin_route_plan(
    turn: Any,
    *,
    turn_id: str,
    provider: str,
    model: str,
    context_window: int,
    capabilities: ModelCapabilities | None,
    effective_thinking: object,
    fallback_capabilities: Mapping[
        tuple[str, str],
        tuple[int, ModelCapabilities | None]
        | tuple[int, int, ModelCapabilities | None],
    ] | None = None,
) -> RoutePlan | None:
    """Create the turn's RoutePlan once and return the already-pinned value later."""

    existing = getattr(turn, "route_plan", None)
    if isinstance(existing, RoutePlan):
        return existing

    metadata = turn.metadata
    tier = _text(metadata.get("routed_tier"))
    if not tier:
        return None

    route_provider = _text(metadata.get("routed_provider")) or _text(provider)
    route_model = _text(metadata.get("routed_model")) or _text(model)
    fallback_candidates: list[object] = []
    for key in ("router_fallback_chain", "selector_execution_chain"):
        value = metadata.get(key)
        if isinstance(value, list):
            fallback_candidates.extend(value)
    plan = RoutePlan(
        version=2,
        plan_id=turn_id,
        turn_id=turn_id,
        tier=tier,
        provider=route_provider,
        model=route_model,
        source=_text(metadata.get("routing_source")) or "none",
        routing_applied=bool(metadata.get("routing_applied", True)),
        thinking=_thinking_snapshot(metadata, effective_thinking),
        prompt_policy=_text(metadata.get("prompt_policy")),
        fallback_chain=_fallback_chain(
            fallback_candidates,
            default_provider=route_provider,
            primary_model=route_model,
            capability_snapshots=fallback_capabilities,
        ),
        capabilities=_capability_snapshot(
            context_window=context_window,
            capabilities=capabilities,
        ),
        router_tier_snapshot=_router_tier_snapshot(
            getattr(turn, "config", None),
            metadata,
            winner_tier=tier,
            winner_provider=route_provider,
            winner_model=route_model,
        ),
    )
    turn.route_plan = plan
    metadata.setdefault("route_plan", plan.as_dict())
    return plan


def record_execution_leg(
    metadata: dict[str, Any] | None,
    *,
    provider: str,
    model: str,
    kind: str,
    config: Any = None,
    reason: str = "",
) -> None:
    """Append one physical provider request without changing the RoutePlan."""

    if metadata is None:
        return
    raw_legs = metadata.setdefault("execution_legs", [])
    if not isinstance(raw_legs, list):
        return
    correlation = getattr(config, "provider_request_correlation", None)
    execution_id = ""
    call_kind = ""
    if isinstance(correlation, ProviderRequestCorrelation):
        execution_id = correlation.execution_id
        call_kind = correlation.call_kind
    plan_snapshot = metadata.get("route_plan")
    plan_id = (
        _text(plan_snapshot.get("plan_id"))
        if isinstance(plan_snapshot, Mapping)
        else ""
    )
    leg: dict[str, Any] = {
        "index": len(raw_legs),
        "kind": kind,
        "provider": _text(provider),
        "model": _text(model),
        "plan_id": plan_id,
    }
    if execution_id:
        leg["execution_id"] = execution_id
    if call_kind:
        leg["call_kind"] = call_kind
    if reason:
        leg["reason"] = reason
    raw_legs.append(leg)


def route_plan_snapshot(turn: Any) -> dict[str, Any] | None:
    plan = getattr(turn, "route_plan", None)
    if isinstance(plan, RoutePlan):
        return plan.as_dict()
    snapshot = turn.metadata.get("route_plan")
    return dict(snapshot) if isinstance(snapshot, Mapping) else None


__all__ = [
    "RouteCapabilitySnapshot",
    "RouteFallback",
    "RoutePlan",
    "RouterTierSnapshot",
    "RouterTierSnapshotEntry",
    "pin_route_plan",
    "record_execution_leg",
    "route_plan_snapshot",
]
