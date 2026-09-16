"""Pure candidate policy shared by primary, Router and config mutations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opensquilla.provider.preset_registry import ProviderPreset, get_preset
from opensquilla.router_tiers import (
    TEXT_TIERS,
    effective_ensemble_selection_mode,
    normalize_tier_mapping,
    router_tier_provider_roles,
)


class LlmProfileActivationError(ValueError):
    """Stable, secret-free validation failure for profile promotion."""

    def __init__(
        self,
        reason: str,
        message: str | None = None,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.reason = reason
        self.details = dict(details or {})
        super().__init__(message or reason)


class RouterProviderConflictError(LlmProfileActivationError):
    """A candidate newly executes a foreign tier without cross-provider routing."""


class PrimaryProviderChangedError(ValueError):
    """The caller's expected primary no longer matches the saved primary."""


def reconcile_recommended_router(
    config: Any,
    provider_id: str,
    *,
    preset: ProviderPreset | None = None,
) -> None:
    """Replace only trusted ladder ownership, preserving all strategy settings."""
    from opensquilla.gateway.config import SquillaRouterConfig

    preset = preset or get_preset(provider_id)
    if preset is None:
        raise ValueError(f"provider {provider_id!r} has no managed router preset")
    payload = config.squilla_router.model_dump(mode="python")
    payload.pop("tiers", None)
    payload["preset_binding"] = "follow_primary"
    if preset.persistable and payload["enabled"]:
        payload["tier_profile"] = provider_id
    else:
        payload["tier_profile"] = None
        tiers = preset.tier_defaults()
        for tier in tiers.values():
            if not str(tier.get("model") or "").strip():
                tier["model"] = str(config.llm.model or "").strip()
        payload["tiers"] = tiers
    config.squilla_router = SquillaRouterConfig(**payload)


def executable_router_dependencies(config: Any) -> set[tuple[str, str, str]]:
    """Identify executable text deployments using the runtime's tier-role policy.

    Retired image_model rows never execute. Enabled observe ladders can still
    serve image admission, so retain their dependencies. Independent global Ensemble
    plans own their lineup and leave text Router deployments dormant.
    """
    router = config.squilla_router
    if not router.enabled:
        return set()
    tiers = normalize_tier_mapping(router.tiers)
    roles = router_tier_provider_roles(
        tiers,
        shared_selection_mode=effective_ensemble_selection_mode(config),
        ensemble_globally_enabled=bool(config.llm_ensemble.enabled),
    )
    return {
        (tier, str(value.get("provider") or config.llm.provider).strip().lower(), roles[tier])
        for tier, value in tiers.items()
        if tier in TEXT_TIERS and roles[tier] in {"direct", "dynamic_member"}
    }


def router_provider_conflicts(config: Any, provider_id: str) -> tuple[str, ...]:
    if config.squilla_router.cross_provider_tiers:
        return ()
    target = provider_id.strip().lower()
    return tuple(
        sorted(
            {
                provider
                for _, provider, _ in executable_router_dependencies(config)
                if provider and provider != target
            }
        )
    )


def validate_router_candidate(
    candidate: Any,
    *,
    allowed_actions: tuple[str, ...] = ("use_recommended",),
) -> None:
    provider = str(candidate.llm.provider).strip().lower()
    conflicts = router_provider_conflicts(candidate, provider)
    if conflicts:
        raise RouterProviderConflictError(
            "router_provider_conflict",
            "Router tiers reference provider(s) that differ from the primary: "
            + ", ".join(conflicts),
            details={
                "reason": "router_provider_conflict",
                "providerId": provider,
                "conflictProviders": list(conflicts),
                "allowedRouterActions": list(allowed_actions),
            },
        )


def validate_router_reactivation(
    previous: Any, candidate: Any, *, explicit_paths: set[str] | None = None,
) -> None:
    """Guard newly executable dependencies; legacy unrelated saves stay valid."""
    if previous is None or candidate.squilla_router.cross_provider_tiers:
        return
    if explicit_paths is not None and not any(
        path == "llm.provider"
        or path.startswith("squilla_router.")
        or path.startswith("llm_ensemble.")
        for path in explicit_paths
    ):
        return
    before = executable_router_dependencies(previous)
    after = executable_router_dependencies(candidate)
    if (
        after - before
        or (
            after
            and previous.squilla_router.rollout_phase == "observe"
            and candidate.squilla_router.rollout_phase != "observe"
        )
        or (after and previous.squilla_router.cross_provider_tiers)
        or (after and previous.llm.provider != candidate.llm.provider)
    ):
        validate_router_candidate(candidate)
