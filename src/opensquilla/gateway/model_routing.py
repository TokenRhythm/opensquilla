"""Canonical Gateway-owned model-routing mode contract.

The WebUI historically derived the effective ``direct | router | ensemble``
mode from three config fields. Keep that policy in the Gateway so every
surface observes and mutates the same state machine.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from opensquilla.router_tiers import (
    CUSTOM_B5_SELECTION_MODE,
    HIGHEST_TEXT_TIER,
    IMAGE_TIER,
    INDEPENDENT_ENSEMBLE_SELECTION_MODES,
    STATIC_B5_PROFILES,
    effective_ensemble_selection_mode,
    ensemble_selection_configured,
    normalize_tier_mapping,
    static_b5_profile,
    tier_ensemble_active,
    tier_index,
)

ModelRoutingMode = Literal["direct", "router", "ensemble"]


def _router_image_route(config: Any) -> tuple[str, dict[str, Any]] | None:
    router = getattr(config, "squilla_router", None)
    tiers = normalize_tier_mapping(getattr(router, "tiers", {}) or {})
    c3_fusion_active = bool(
        getattr(getattr(config, "llm_ensemble", None), "enabled", False)
    ) or tier_ensemble_active(tiers, HIGHEST_TEXT_TIER)
    image_tiers = {
        name: tier
        for name, tier in tiers.items()
        if isinstance(tier, dict)
        and bool(tier.get("supports_image", False))
        and bool(str(tier.get("model") or "").strip())
        and not (c3_fusion_active and name == HIGHEST_TEXT_TIER)
    }
    if not image_tiers:
        return None
    ordered = sorted(
        image_tiers,
        key=lambda name: (tier_index(name) < 0, tier_index(name)),
    )
    if IMAGE_TIER in image_tiers:
        ordered = [IMAGE_TIER, *(name for name in ordered if name != IMAGE_TIER)]
    selected = ordered[0]
    return selected, image_tiers[selected]


def _deployment_vision_support(
    *,
    model: str,
    provider: str,
    api_key: str = "",
    base_url: str = "",
    proxy: str = "",
) -> str:
    if not model:
        return "unknown"
    try:
        from opensquilla.provider.model_catalog import shared_catalog

        resolver = getattr(shared_catalog(), "resolve_deployment_vision_support", None)
        if callable(resolver):
            resolved = resolver(
                model,
                provider=provider,
                api_key=api_key,
                base_url=base_url,
                proxy=proxy,
            )
            if isinstance(resolved, str) and resolved in {
                "supported",
                "unsupported",
                "unknown",
            }:
                return resolved
    except Exception:  # noqa: BLE001 - public snapshot must remain available
        pass
    return "unknown"


def _image_input_routing_snapshot(
    config: Any,
    *,
    router_enabled: bool,
    ensemble_enabled: bool,
    selection_mode: str,
) -> dict[str, str]:
    independent_ensemble = bool(
        ensemble_enabled and selection_mode in INDEPENDENT_ENSEMBLE_SELECTION_MODES
    )
    if independent_ensemble or (ensemble_enabled and not router_enabled):
        return {
            "admission": "blocked",
            "reason": "ensemble_mode_unsupported",
        }
    if router_enabled:
        image_route = _router_image_route(config)
        if image_route is None:
            return {
                "admission": "blocked",
                "reason": "router_image_route_unavailable",
            }
        _, image_tier = image_route
        llm = getattr(config, "llm", None)
        active_provider = _clean(getattr(llm, "provider", ""))
        tier_provider = _clean(image_tier.get("provider"))
        cross_provider = bool(
            getattr(getattr(config, "squilla_router", None), "cross_provider_tiers", False)
        )
        provider = tier_provider if cross_provider and tier_provider else active_provider
        provider = provider or tier_provider
        use_active_authority = not provider or provider == active_provider
        vision_support = _deployment_vision_support(
            model=_clean(image_tier.get("model")),
            provider=provider,
            api_key=str(getattr(llm, "api_key", "") or "") if use_active_authority else "",
            base_url=str(getattr(llm, "base_url", "") or "") if use_active_authority else "",
            proxy=str(getattr(llm, "proxy", "") or "") if use_active_authority else "",
        )
        if vision_support == "supported":
            return {
                "admission": "allowed",
                "reason": "router_image_route_available",
            }
        if vision_support == "unsupported":
            return {
                "admission": "blocked",
                "reason": "model_vision_unsupported",
            }
        return {"admission": "unknown", "reason": "capability_unknown"}

    llm = getattr(config, "llm", None)
    model = _clean(getattr(llm, "model", ""))
    provider = _clean(getattr(llm, "provider", ""))
    vision_support = _deployment_vision_support(
        model=model,
        provider=provider,
        api_key=str(getattr(llm, "api_key", "") or ""),
        base_url=str(getattr(llm, "base_url", "") or ""),
        proxy=str(getattr(llm, "proxy", "") or ""),
    )
    if vision_support == "supported":
        return {"admission": "allowed", "reason": "model_vision_supported"}
    if vision_support == "unsupported":
        return {
            "admission": "blocked",
            "reason": "model_vision_unsupported",
        }
    return {"admission": "unknown", "reason": "capability_unknown"}


_DURABLE_ROUTER_TIER_TEXT_FIELDS = frozenset(
    {
        "provider",
        "model",
        "description",
        "thinking_level",
        "thinkingLevel",
        "ensemble_selection_mode",
        "ensembleSelectionMode",
    }
)
_DURABLE_ROUTER_TIER_BOOL_FIELDS = frozenset(
    {
        "supports_image",
        "supportsImage",
        "supports_thinking",
        "image_only",
        "imageOnly",
        "ensemble_enabled",
        "ensembleEnabled",
    }
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
    # ``None`` means the snapshot came from the global policy.  Interactive
    # session snapshots carry the persisted, explicit mode that was resolved
    # at acceptance.  This remains diagnostic metadata only: TurnRunner uses
    # the two routing subtrees above and never consults this field to mutate
    # live configuration.
    session_mode: ModelRoutingMode | None = None
    # Resolution metadata is frozen with the effective mode so task audit can
    # explain whether a turn used a persisted session choice or the global
    # policy.  It is never used by the execution overlay itself.
    session_routing_revision: int | None = None
    session_routing_source: str = "global_policy"

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


def _custom_candidate(
    provider: str,
    model: str,
    *,
    role: str = "proposer",
) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "source": "custom",
        "enabled": True,
        "role": role,
        "thinking_level": "",
    }


def _provider_is_runtime_supported(provider: str) -> bool:
    """Whether a provider can structurally participate in model execution."""

    from opensquilla.provider.registry import UnknownProviderError, get_provider_spec

    try:
        return bool(get_provider_spec(provider).runtime_supported)
    except UnknownProviderError:
        return False


def _provider_ensemble_candidates(config: Any) -> list[dict[str, Any]]:
    """Build the provider-aware first-activation custom lineup."""

    provider = _clean(getattr(getattr(config, "llm", None), "provider", ""))
    static_profile = next(
        (
            profile
            for profile in STATIC_B5_PROFILES.values()
            if _clean(profile.provider_id) == provider
        ),
        None,
    )
    if static_profile is not None:
        static_candidates = [
            _custom_candidate(static_profile.provider_id, model)
            for model in static_profile.proposer_models
        ]
        static_candidates.append(
            _custom_candidate(
                static_profile.provider_id,
                static_profile.aggregator_model,
                role="aggregator",
            )
        )
        return static_candidates

    router = getattr(config, "squilla_router", None)
    tiers = getattr(router, "tiers", {}) or {}
    tier_order = ("c0", "c1", "c2", "c3")
    seen: set[tuple[str, str]] = set()
    candidates: list[dict[str, Any]] = []
    if isinstance(tiers, dict):
        normalized: dict[str, dict[str, Any]] = {}
        for raw_tier, raw_cfg in tiers.items():
            tier = _clean(raw_tier)
            if tier.startswith("t") and tier[1:] in {"0", "1", "2", "3"}:
                tier = f"c{tier[1:]}"
            if tier in tier_order and isinstance(raw_cfg, dict):
                normalized[tier] = raw_cfg
        for tier in tier_order:
            tier_cfg = normalized.get(tier)
            if not tier_cfg:
                continue
            member_provider = _clean(tier_cfg.get("provider") or provider)
            model = str(tier_cfg.get("model") or "").strip()
            identity = (member_provider, model)
            if (
                not member_provider
                or not model
                or identity in seen
                or not _provider_is_runtime_supported(member_provider)
            ):
                continue
            seen.add(identity)
            candidates.append(_custom_candidate(member_provider, model))
            if len(candidates) >= 6:
                break
    if len(candidates) < 2:
        raise ValueError(
            "Ensemble needs at least two distinct runtime-supported Router tier candidates; "
            "configure the lineup before enabling it"
        )
    return candidates


def ensemble_activation_patches(config: Any) -> dict[str, Any]:
    """Return first-activation selection/candidate patches, or none."""

    ensemble = getattr(config, "llm_ensemble", None)
    if ensemble is None or bool(getattr(ensemble, "enabled", False)):
        return {}
    if ensemble_selection_configured(config):
        return {}
    selection_mode = effective_ensemble_selection_mode(config)
    if selection_mode != CUSTOM_B5_SELECTION_MODE:
        return {"llm_ensemble.selection_mode": selection_mode}
    return {
        "llm_ensemble.selection_mode": CUSTOM_B5_SELECTION_MODE,
        "llm_ensemble.candidates": _provider_ensemble_candidates(config),
    }


def ensemble_activation_preview(config: Any) -> dict[str, Any]:
    """Return a non-secret preview of the current or planned Ensemble lineup."""

    ensemble = getattr(config, "llm_ensemble", None)
    configured = ensemble_selection_configured(config)
    if ensemble is None:
        return {
            "selection_mode": "",
            "proposer_count": 0,
            "member_providers": [],
            "candidates": [],
            "blocked_reason": "llm_ensemble_missing",
        }
    try:
        patches = ensemble_activation_patches(config)
    except ValueError as exc:
        return {
            "selection_mode": CUSTOM_B5_SELECTION_MODE,
            "proposer_count": 0,
            "member_providers": [],
            "candidates": [],
            "blocked_reason": str(exc),
        }
    selection_mode = str(
        patches.get("llm_ensemble.selection_mode")
        or getattr(ensemble, "selection_mode", "")
    )
    static_profile = static_b5_profile(selection_mode)
    if static_profile is not None:
        return {
            "selection_mode": selection_mode,
            "selection_configured": configured,
            "proposer_count": len(static_profile.proposer_models),
            "member_providers": [static_profile.provider_id],
            "candidates": [],
            "blocked_reason": None,
        }
    candidates = patches.get("llm_ensemble.candidates")
    if candidates is None:
        candidates = [
            candidate.model_dump(mode="python")
            if hasattr(candidate, "model_dump")
            else dict(candidate)
            for candidate in list(getattr(ensemble, "candidates", []) or [])
        ]
    enabled_candidates = [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("enabled", True) is not False
    ]
    proposers = [
        candidate
        for candidate in enabled_candidates
        if str(candidate.get("role") or "") != "aggregator"
    ]
    providers = sorted(
        {
            _clean(candidate.get("provider"))
            for candidate in enabled_candidates
            if _clean(candidate.get("provider"))
        }
    )
    return {
        "selection_mode": selection_mode,
        "selection_configured": configured,
        "proposer_count": len(proposers),
        "member_providers": providers,
        "candidates": enabled_candidates,
        "blocked_reason": None,
    }


def model_routing_snapshot(config: Any) -> dict[str, Any]:
    """Return the additive public snapshot for the current runtime strategy."""

    router = getattr(config, "squilla_router", None)
    ensemble = getattr(config, "llm_ensemble", None)
    router_enabled = bool(getattr(router, "enabled", False))
    ensemble_enabled = bool(getattr(ensemble, "enabled", False))
    rollout_phase = _clean(getattr(router, "rollout_phase", "observe")) or "observe"
    selection_mode = _clean(getattr(ensemble, "selection_mode", ""))
    router_required = selection_mode not in INDEPENDENT_ENSEMBLE_SELECTION_MODES

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
        "selection_configured": ensemble_selection_configured(config),
        "activation_preview": ensemble_activation_preview(config),
        "router_required_by_ensemble": router_required,
        "image_input": _image_input_routing_snapshot(
            config,
            router_enabled=router_enabled,
            ensemble_enabled=ensemble_enabled,
            selection_mode=selection_mode,
        ),
        "applies_to": "next_accepted_turn",
    }


def model_routing_patches(
    config: Any,
    mode: str,
    *,
    activation_config: Any = None,
) -> dict[str, Any]:
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

    planner_config = activation_config if activation_config is not None else config
    patches = ensemble_activation_patches(planner_config)
    selection_mode = _clean(
        patches.get("llm_ensemble.selection_mode")
        or getattr(getattr(config, "llm_ensemble", None), "selection_mode", "")
    )
    return {
        **patches,
        "llm_ensemble.enabled": True,
        "squilla_router.enabled": selection_mode not in INDEPENDENT_ENSEMBLE_SELECTION_MODES,
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


def apply_model_routing_mode(
    config: Any,
    mode: str,
    *,
    activation_config: Any = None,
) -> dict[str, Any]:
    """Apply one canonical mode to a config-like object in place.

    The returned mapping contains only fields whose values changed.  All three
    owned paths are force-persisted when the config supports sparse persistence,
    so a derived ``false``/``observe`` value cannot disappear on restart.
    """

    changed: dict[str, Any] = {}
    for path, value in model_routing_patches(
        config,
        mode,
        activation_config=activation_config,
    ).items():
        section_name, field_name = path.split(".", 1)
        section = getattr(config, section_name, None)
        if section is None:
            continue
        if path == "llm_ensemble.candidates":
            from opensquilla.gateway.config import LlmEnsembleCandidateConfig

            value = [
                candidate
                if isinstance(candidate, LlmEnsembleCandidateConfig)
                else LlmEnsembleCandidateConfig.model_validate(candidate)
                for candidate in list(value or [])
            ]
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
        activation_config = (
            previous
            if (
                mode == "ensemble"
                and previous is not None
                and "llm_ensemble.selection_mode" not in explicit_paths
            )
            else None
        )
        return apply_model_routing_mode(
            config,
            mode,
            activation_config=activation_config,
        )

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


def capture_model_routing_config(
    config: Any,
    *,
    session_mode: ModelRoutingMode | str | None = None,
    session_routing_revision: int | None = None,
    session_routing_source: str | None = None,
) -> Any:
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
    normalized_mode = _clean(session_mode)
    if not normalized_mode:
        return _ModelRoutingConfigSnapshot(
            squilla_router=copy.deepcopy(getattr(config, "squilla_router", None)),
            llm_ensemble=copy.deepcopy(getattr(config, "llm_ensemble", None)),
            session_routing_revision=session_routing_revision,
            session_routing_source=(
                session_routing_source or "global_policy"
            ),
        )
    if normalized_mode not in {"direct", "router", "ensemble"}:
        raise ValueError("session_mode must be direct, router, ensemble, or None")

    # Do not apply the per-session control to the shared GatewayConfig.  A
    # small config-like copy is sufficient because ``apply_model_routing_mode``
    # only owns these two subtrees; it also retains the normal Ensemble
    # activation planner through ``activation_config``.
    class _RoutingOverlay:
        squilla_router: Any
        llm_ensemble: Any

        def __init__(self) -> None:
            self.squilla_router = copy.deepcopy(
                getattr(config, "squilla_router", None)
            )
            self.llm_ensemble = copy.deepcopy(
                getattr(config, "llm_ensemble", None)
            )

    overlay = _RoutingOverlay()
    apply_model_routing_mode(
        overlay,
        normalized_mode,
        activation_config=config,
    )
    return _ModelRoutingConfigSnapshot(
        squilla_router=overlay.squilla_router,
        llm_ensemble=overlay.llm_ensemble,
        session_mode=cast(ModelRoutingMode, normalized_mode),
        session_routing_revision=session_routing_revision,
        session_routing_source=(
            session_routing_source or "session_persisted"
        ),
    )


def durable_model_routing_config_snapshot(config: Any) -> dict[str, Any] | None:
    """Serialize one accepted routing snapshot for exact restart recovery.

    Persisting a strict, non-secret projection keeps Router tiers and Ensemble
    lineups frozen for already-accepted work without extending that freeze to
    live safety, tool, channel, or approval policy.
    """

    if not isinstance(config, _ModelRoutingConfigSnapshot):
        return None
    payload: dict[str, Any] = {}
    for field_name in ("squilla_router", "llm_ensemble"):
        value = getattr(config, field_name)
        if value is None:
            return None
        model_dump = getattr(value, "model_dump", None)
        if not callable(model_dump):
            return None
        dumped = model_dump(mode="json")
        if not isinstance(dumped, dict):
            return None
        if field_name == "squilla_router":
            dumped["tiers"] = _durable_router_tier_snapshot(dumped.get("tiers"))
        payload[field_name] = dumped
    return payload


def _durable_router_tier_snapshot(value: object) -> dict[str, dict[str, Any]]:
    from opensquilla.router_tiers import normalize_tier_id, normalize_tier_mapping

    if not isinstance(value, Mapping):
        return {}
    snapshot: dict[str, dict[str, Any]] = {}
    for raw_name, raw_tier in normalize_tier_mapping(value).items():
        tier_name = normalize_tier_id(raw_name)
        if tier_name is None or not isinstance(raw_tier, Mapping):
            continue
        tier: dict[str, Any] = {}
        for field_name in _DURABLE_ROUTER_TIER_TEXT_FIELDS:
            field_value = raw_tier.get(field_name)
            if isinstance(field_value, str):
                tier[field_name] = field_value
        thinking = raw_tier.get("thinking")
        if isinstance(thinking, (str, bool)):
            tier["thinking"] = thinking
        for field_name in _DURABLE_ROUTER_TIER_BOOL_FIELDS:
            field_value = raw_tier.get(field_name)
            if isinstance(field_value, bool):
                tier[field_name] = field_value
        snapshot[tier_name] = tier
    return snapshot


def restore_durable_model_routing_config_snapshot(
    payload: dict[str, Any],
    *,
    session_mode: ModelRoutingMode | str | None,
    session_routing_revision: int | None,
    session_routing_source: str,
) -> Any:
    """Validate and restore a server-written durable routing snapshot."""

    if set(payload) != {"squilla_router", "llm_ensemble"}:
        raise ValueError("invalid durable model-routing config snapshot")
    router_payload = payload.get("squilla_router")
    ensemble_payload = payload.get("llm_ensemble")
    if not isinstance(router_payload, dict) or not isinstance(ensemble_payload, dict):
        raise ValueError("invalid durable model-routing config snapshot")
    if router_payload.get("tiers") != _durable_router_tier_snapshot(
        router_payload.get("tiers")
    ):
        raise ValueError("invalid durable model-routing tier snapshot")

    from opensquilla.gateway.config import LlmEnsembleConfig, SquillaRouterConfig

    normalized_mode = _clean(session_mode)
    if normalized_mode and normalized_mode not in {"direct", "router", "ensemble"}:
        raise ValueError("invalid durable model-routing session mode")
    return _ModelRoutingConfigSnapshot(
        squilla_router=SquillaRouterConfig.model_validate(router_payload),
        llm_ensemble=LlmEnsembleConfig.model_validate(ensemble_payload),
        session_mode=(
            cast(ModelRoutingMode, normalized_mode) if normalized_mode else None
        ),
        session_routing_revision=session_routing_revision,
        session_routing_source=session_routing_source,
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
    "durable_model_routing_config_snapshot",
    "model_routing_mode_for_write",
    "model_routing_patches",
    "model_routing_snapshot",
    "reconcile_model_routing_write",
    "restore_durable_model_routing_config_snapshot",
]
