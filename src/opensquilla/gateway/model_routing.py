"""Canonical Gateway-owned model-routing mode contract.

The WebUI historically derived the effective ``direct | router | ensemble``
mode from three config fields. Keep that policy in the Gateway so every
surface observes and mutates the same state machine.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal

ModelRoutingMode = Literal["direct", "router", "ensemble"]

_INDEPENDENT_ENSEMBLE_MODES = frozenset(
    {"static_openrouter_b5", "static_tokenrhythm_b5", "custom_b5"}
)


@dataclass(frozen=True, slots=True)
class _ModelRoutingConfigSnapshot:
    """Acceptance-time values for the two routing-owned config subtrees.

    No other Gateway config belongs in this snapshot.  In particular, tool,
    agent, channel, approval, and safety policy must remain live so a queued
    turn cannot bypass a policy hot-apply that landed before it began running.
    ``TurnRunner._turn_config`` calls :meth:`overlay_live_config` at execution
    time to combine these two frozen values with the latest live config.
    """

    squilla_router: Any
    llm_ensemble: Any

    def overlay_live_config(self, live_config: Any) -> Any:
        """Overlay only routing fields onto the latest live Gateway config."""

        if live_config is None:
            return self
        update = {
            "squilla_router": self.squilla_router,
            "llm_ensemble": self.llm_ensemble,
        }
        model_copy = getattr(live_config, "model_copy", None)
        if callable(model_copy):
            return model_copy(update=update, deep=False)
        overlay = copy.copy(live_config)
        for field_name, value in update.items():
            setattr(overlay, field_name, value)
        return overlay


def _clean(value: object) -> str:
    return str(value or "").strip().lower()


def model_routing_snapshot(config: Any) -> dict[str, Any]:
    """Return the additive public snapshot for the current runtime strategy."""

    router = getattr(config, "squilla_router", None)
    ensemble = getattr(config, "llm_ensemble", None)
    router_enabled = bool(getattr(router, "enabled", False))
    ensemble_enabled = bool(getattr(ensemble, "enabled", False))
    rollout_phase = _clean(getattr(router, "rollout_phase", "observe")) or "observe"
    selection_mode = _clean(getattr(ensemble, "selection_mode", ""))
    router_required = selection_mode not in _INDEPENDENT_ENSEMBLE_MODES

    if ensemble_enabled:
        mode: ModelRoutingMode = "ensemble"
    elif router_enabled and rollout_phase != "observe":
        mode = "router"
    else:
        mode = "direct"

    return {
        "mode": mode,
        "router_enabled": router_enabled,
        "ensemble_enabled": ensemble_enabled,
        "rollout_phase": rollout_phase,
        "selection_mode": selection_mode,
        "router_required_by_ensemble": router_required,
        "applies_to": "next_accepted_turn",
    }


def model_routing_patches(config: Any, mode: str) -> dict[str, Any]:
    """Translate one public mode into the persisted config patch contract."""

    normalized = _clean(mode)
    if normalized not in {"direct", "router", "ensemble"}:
        raise ValueError("params.mode must be direct, router, or ensemble")

    if normalized == "direct":
        return {
            "llm_ensemble.enabled": False,
            "squilla_router.enabled": False,
            "squilla_router.rollout_phase": "observe",
        }
    if normalized == "router":
        return {
            "llm_ensemble.enabled": False,
            "squilla_router.enabled": True,
            "squilla_router.rollout_phase": "full",
        }

    selection_mode = _clean(
        getattr(getattr(config, "llm_ensemble", None), "selection_mode", "")
    )
    return {
        "llm_ensemble.enabled": True,
        "squilla_router.enabled": selection_mode not in _INDEPENDENT_ENSEMBLE_MODES,
        "squilla_router.rollout_phase": "full",
    }


def _path_was_written(explicit_paths: set[str], target: str) -> bool:
    """Return whether the concrete control leaf was submitted.

    Nested-payload path collectors also report parent objects.  Matching those
    parents would make an unrelated edit such as ``squilla_router.default_tier``
    select a model-routing mode, so only the actual owned leaf counts.
    """

    return target in explicit_paths


def _control_leaf_changed(
    config: Any,
    previous: Any,
    section_name: str,
    field_name: str,
) -> bool:
    """Whether a written control boolean differs from its pre-write value.

    ``previous`` is the config as it stood before the write; ``None`` means
    the caller has no pre-write snapshot, in which case every explicit write
    conservatively counts as a change (the legacy interpretation).
    """

    if previous is None:
        return True
    new_value = bool(getattr(getattr(config, section_name, None), field_name, False))
    old_value = bool(getattr(getattr(previous, section_name, None), field_name, False))
    return new_value != old_value


def model_routing_mode_for_write(
    config: Any,
    explicit_paths: set[str],
    *,
    previous: Any = None,
) -> ModelRoutingMode | None:
    """Translate legacy routing-field writes into the canonical three-state mode.

    Older surfaces write the Router and Ensemble booleans directly.  Treat a
    single explicit boolean as the corresponding three-state control, while a
    complete multi-field write (such as ``models.routing.set``) is interpreted
    from its final candidate values.  Non-control settings such as router tiers
    or ensemble candidates do not select a mode.

    When ``previous`` (the pre-write config) is supplied, a value-identical
    re-assertion of a control boolean selects no mode: re-saving
    ``llm_ensemble.enabled=false`` while the Router runs must not disable the
    Router, and re-saving ``squilla_router.enabled=true`` must not escalate an
    advanced ``rollout_phase`` back to ``full``.
    """

    ensemble_enabled_written = _path_was_written(
        explicit_paths, "llm_ensemble.enabled"
    )
    router_enabled_written = _path_was_written(
        explicit_paths, "squilla_router.enabled"
    )
    ensemble_enabled_toggled = ensemble_enabled_written and _control_leaf_changed(
        config, previous, "llm_ensemble", "enabled"
    )
    router_enabled_toggled = router_enabled_written and _control_leaf_changed(
        config, previous, "squilla_router", "enabled"
    )
    if (ensemble_enabled_written or router_enabled_written) and not (
        ensemble_enabled_toggled or router_enabled_toggled
    ):
        return None
    if ensemble_enabled_written and router_enabled_written:
        ensemble_enabled = bool(
            getattr(getattr(config, "llm_ensemble", None), "enabled", False)
        )
        router_enabled = bool(
            getattr(getattr(config, "squilla_router", None), "enabled", False)
        )
        if ensemble_enabled:
            return "ensemble"
        if router_enabled:
            return "router"
        return "direct"
    if ensemble_enabled_written:
        enabled = bool(
            getattr(getattr(config, "llm_ensemble", None), "enabled", False)
        )
        return "ensemble" if enabled else "direct"
    if router_enabled_written:
        enabled = bool(
            getattr(getattr(config, "squilla_router", None), "enabled", False)
        )
        return "router" if enabled else "direct"
    return None


def apply_model_routing_mode(config: Any, mode: str) -> dict[str, Any]:
    """Apply one canonical mode to a config-like object in place.

    The returned mapping contains only fields whose values changed.  All three
    owned paths are force-persisted when the config supports sparse persistence,
    so a derived ``false``/``observe`` value cannot disappear on restart.
    """

    changed: dict[str, Any] = {}
    for path, value in model_routing_patches(config, mode).items():
        section_name, field_name = path.split(".", 1)
        section = getattr(config, section_name, None)
        if section is None:
            continue
        if getattr(section, field_name, None) != value:
            setattr(section, field_name, value)
            changed[path] = value
        marker = getattr(config, "mark_force_persist", None)
        if callable(marker):
            marker(path)
    return changed


def reconcile_model_routing_write(
    config: Any,
    explicit_paths: set[str],
    *,
    previous: Any = None,
) -> dict[str, Any]:
    """Reconcile only strategy fields owned by a legacy config write.

    Boolean Router/Ensemble toggles select a canonical mode; with a
    ``previous`` snapshot supplied, value-identical re-assertions select
    none.  A live Ensemble ``selection_mode`` edit only updates whether that
    implementation requires Router; it deliberately preserves advanced
    ``rollout_phase`` values such as ``prompt_only``.  Other Router/Ensemble
    settings are left untouched.
    """

    mode = model_routing_mode_for_write(config, explicit_paths, previous=previous)
    if mode is not None:
        return apply_model_routing_mode(config, mode)

    if (
        "llm_ensemble.selection_mode" not in explicit_paths
        or not bool(getattr(getattr(config, "llm_ensemble", None), "enabled", False))
    ):
        return {}

    required = model_routing_patches(config, "ensemble")["squilla_router.enabled"]
    router = getattr(config, "squilla_router", None)
    if router is None or getattr(router, "enabled", None) == required:
        return {}
    router.enabled = required
    marker = getattr(config, "mark_force_persist", None)
    if callable(marker):
        marker("squilla_router.enabled")
    return {"squilla_router.enabled": required}


def capture_model_routing_config(config: Any) -> Any:
    """Freeze model-routing inputs at the turn acceptance boundary.

    Gateway config writes update the long-lived config object in place.  A
    queued/running turn must not observe a half-new strategy merely because a
    surface switches ``direct | router | ensemble`` while that turn is being
    prepared.  Capture only the two routing subtrees.  The TurnRunner overlays
    them onto the latest live config at execution time, so unrelated policy
    hot-applies are never frozen at acceptance.
    """

    if config is None:
        return None
    return _ModelRoutingConfigSnapshot(
        squilla_router=copy.deepcopy(getattr(config, "squilla_router", None)),
        llm_ensemble=copy.deepcopy(getattr(config, "llm_ensemble", None)),
    )


async def broadcast_model_routing_changed(
    ctx: Any,
    *,
    source: str,
    config: Any | None = None,
) -> dict[str, Any]:
    """Broadcast the canonical snapshot to every readable operator surface."""

    active_config = config if config is not None else getattr(ctx, "config", None)
    snapshot = model_routing_snapshot(active_config)
    payload = {**snapshot, "source": source}
    subscription_manager = getattr(ctx, "subscription_manager", None)
    if subscription_manager is None:
        return payload

    # Local imports avoid making websocket boot order part of config loading.
    from opensquilla.gateway.event_bridge import EventBridge
    from opensquilla.gateway.scopes import READ_SCOPE
    from opensquilla.gateway.websocket import get_registry

    await EventBridge(subscription_manager, get_registry()).broadcast_scoped(
        "models.routing.changed",
        payload,
        required_scope=READ_SCOPE,
    )
    return payload


async def broadcast_model_routing_changed_if_needed(
    ctx: Any,
    *,
    previous: dict[str, Any],
    source: str,
    config: Any | None = None,
) -> dict[str, Any] | None:
    """Broadcast only when the canonical routing snapshot actually changed.

    Config hot-apply handlers mutate the long-lived Gateway config object in
    place.  Callers therefore capture ``previous`` before the write and pass
    the successfully applied config here afterwards.  Comparing the complete
    public snapshot keeps every config entry point aligned without guessing
    which individual fields might affect the routing state machine.
    """

    active_config = config if config is not None else getattr(ctx, "config", None)
    current = model_routing_snapshot(active_config)
    if current == previous:
        return None
    return await broadcast_model_routing_changed(
        ctx,
        source=source,
        config=active_config,
    )


__all__ = [
    "ModelRoutingMode",
    "apply_model_routing_mode",
    "broadcast_model_routing_changed",
    "broadcast_model_routing_changed_if_needed",
    "capture_model_routing_config",
    "model_routing_mode_for_write",
    "model_routing_patches",
    "model_routing_snapshot",
    "reconcile_model_routing_write",
]
