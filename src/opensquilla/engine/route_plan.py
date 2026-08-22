"""Immutable logical routing plan and append-only execution-leg telemetry.

The router decides once, before the agent loop starts.  Provider retries and
selector failover are physical execution details of that decision; they must
not be represented as additional router decisions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from opensquilla.provider.types import ModelCapabilities, ProviderRequestCorrelation


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

    def as_dict(self) -> dict[str, Any]:
        return {
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
        version=1,
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
    "pin_route_plan",
    "record_execution_leg",
    "route_plan_snapshot",
]
