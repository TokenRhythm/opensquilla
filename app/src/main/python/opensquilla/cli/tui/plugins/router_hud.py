"""Router decision HUD plugin for TUI surfaces."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from opensquilla import router_tiers
from opensquilla.cli.tui.backend.domain_events import (
    KIND_ROUTER_DECISION,
    TuiDomainEvent,
)
from opensquilla.cli.tui.backend.plugins import TuiPluginContext

ROUTER_HUD_SLOT = "router_hud"


@dataclass(frozen=True)
class RouterHudSnapshot:
    tier: str
    tier_index: int
    model: str
    baseline_model: str
    source: str
    confidence: float | None
    savings_pct: float | None
    fallback: bool
    thinking_mode: str
    prompt_policy: str
    routing_applied: bool
    rollout_phase: str
    context_window: int | None
    label: str
    style: str


class RouterHudPlugin:
    plugin_id = "router-hud"
    slots = frozenset({ROUTER_HUD_SLOT})

    def __init__(
        self,
        *,
        on_snapshot: Callable[[RouterHudSnapshot], None] | None = None,
    ) -> None:
        self._snapshot: RouterHudSnapshot | None = None
        self._on_snapshot = on_snapshot

    def on_event(self, event: TuiDomainEvent, context: TuiPluginContext) -> None:
        del context
        if event.kind != KIND_ROUTER_DECISION:
            return
        snapshot = build_router_hud_snapshot(event.payload)
        self._snapshot = snapshot
        # Surfaces without a toolbar slot (the plain terminal) subscribe here to
        # render each decision themselves instead of reading the toolbar snapshot.
        if self._on_snapshot is not None:
            self._on_snapshot(snapshot)

    def snapshot(self, slot: str) -> object | None:
        if slot != ROUTER_HUD_SLOT:
            return None
        return self._snapshot


def build_router_hud_snapshot(payload: Mapping[str, Any]) -> RouterHudSnapshot:
    tier = _string_field(payload, "tier")
    # Canonical tiers are c0-c3 with legacy t0-t3 aliases; the shared helper
    # accepts both, so a payload missing tier_index still resolves either way.
    tier_index = _int_field(payload, "tier_index", router_tiers.tier_index(tier))
    model = _string_field(payload, "model")
    baseline_model = _string_field(payload, "baseline_model")
    source = _string_field(payload, "source", "none")
    confidence = _float_field(payload, "confidence")
    savings_pct = _float_field(payload, "savings_pct")
    fallback = _bool_field(payload, "fallback", source == "fallback")
    thinking_mode = _string_field(payload, "thinking_mode")
    prompt_policy = _string_field(payload, "prompt_policy")
    routing_applied = _bool_field(payload, "routing_applied", True)
    rollout_phase = _string_field(payload, "rollout_phase", "full")
    context_window = _optional_int_field(payload, "context_window")
    style = _style_for(
        fallback=fallback,
        routing_applied=routing_applied,
        rollout_phase=rollout_phase,
    )
    label = _label_for(
        tier=tier,
        model=model,
        baseline_model=baseline_model,
        source=source,
        confidence=confidence,
        savings_pct=savings_pct,
        fallback=fallback,
        routing_applied=routing_applied,
        rollout_phase=rollout_phase,
    )
    return RouterHudSnapshot(
        tier=tier,
        tier_index=tier_index,
        model=model,
        baseline_model=baseline_model,
        source=source,
        confidence=confidence,
        savings_pct=savings_pct,
        fallback=fallback,
        thinking_mode=thinking_mode,
        prompt_policy=prompt_policy,
        routing_applied=routing_applied,
        rollout_phase=rollout_phase,
        context_window=context_window,
        label=label,
        style=style,
    )


def _label_for(
    *,
    tier: str,
    model: str,
    baseline_model: str,
    source: str,
    confidence: float | None,
    savings_pct: float | None,
    fallback: bool,
    routing_applied: bool,
    rollout_phase: str,
) -> str:
    model_label = _model_label(model)
    if fallback:
        return f"fallback -> {model_label}"
    if not routing_applied or rollout_phase == "observe":
        prefix = f"observe {tier}".strip()
        return _join_label(prefix, model_label, confidence, savings_pct=None)
    if source == "forced":
        prefix = f"forced {tier}".strip()
        return _join_label(prefix, model_label, confidence, savings_pct=None)
    return _join_label(
        f"route {tier}".strip(),
        model_label,
        confidence,
        savings_pct=savings_pct if baseline_model else None,
    )


def _join_label(
    prefix: str,
    model_label: str,
    confidence: float | None,
    *,
    savings_pct: float | None,
) -> str:
    parts = [f"{prefix} -> {model_label}"]
    if confidence is not None:
        parts.append(f"{round(confidence * 100):.0f}%")
    if savings_pct is not None:
        parts.append(f"save {round(savings_pct):.0f}%")
    return " ".join(parts)


def _style_for(
    *,
    fallback: bool,
    routing_applied: bool,
    rollout_phase: str,
) -> str:
    if fallback:
        return "warning"
    if not routing_applied or rollout_phase == "observe":
        return "dim"
    return "normal"


def _model_label(model: str) -> str:
    return model.rsplit("/", 1)[-1] if model else "unknown"


def _string_field(payload: Mapping[str, Any], key: str, default: str = "") -> str:
    value = payload.get(key, default)
    if value is None:
        return default
    return str(value)


def _bool_field(payload: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = payload.get(key)
    if value is None:
        return default
    return bool(value)


def _float_field(payload: Mapping[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if isinstance(value, int | float):
        return float(value)
    return None


def _int_field(payload: Mapping[str, Any], key: str, default: int) -> int:
    value = payload.get(key)
    if isinstance(value, int):
        return value
    return default


def _optional_int_field(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None
