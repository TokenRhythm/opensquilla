"""Mutations for provider/channel onboarding configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal, cast, get_args

from pydantic import ValidationError

from opensquilla.channels.registry import discover_all, parse_channel_entry
from opensquilla.gateway.config import (
    AudioConfig,
    ChannelsConfig,
    GatewayConfig,
    ImageGenerationConfig,
    LlmEnsembleConfig,
    LlmProviderConfig,
    LlmProviderProfile,
    MemoryEmbeddingConfig,
    SquillaRouterConfig,
    _default_tiers,
)
from opensquilla.gateway.config_secrets import (
    clear_runtime_secret_paths,
    forget_secret_provenance_paths,
    inherit_runtime_secrets,
)
from opensquilla.gateway.model_routing import (
    apply_model_routing_mode,
    ensemble_activation_patches,
    reconcile_model_routing_write,
)
from opensquilla.onboarding.audio_specs import get_audio_provider_setup_spec
from opensquilla.onboarding.endpoint_identity import base_url_allows_credential_reuse
from opensquilla.onboarding.image_generation_specs import (
    get_image_generation_provider_setup_spec,
)
from opensquilla.onboarding.image_generation_state import (
    apply_image_generation_intent,
)
from opensquilla.onboarding.provider_specs import get_provider_setup_spec
from opensquilla.onboarding.redaction import (
    REDACTED_PLACEHOLDER,
    is_redacted_secret_sentinel,
    redact_audio_payload,
    redact_channel_entry,
    redact_error_text,
    redact_image_generation_payload,
    redact_memory_embedding_payload,
    redact_provider_payload,
    redact_router_tiers_payload,
    redact_search_payload,
)
from opensquilla.onboarding.search_specs import get_search_provider_setup_spec
from opensquilla.provider.environment import environment_value
from opensquilla.provider.image_generation_credentials import (
    resolve_image_generation_credential,
)
from opensquilla.provider.image_generation_policy import (
    conflicting_image_generation_endpoint_provider,
    is_valid_image_generation_base_url,
    parse_image_generation_model_ref,
    resolve_image_generation_base_url,
)
from opensquilla.provider.preset_registry import ProviderPreset, get_preset
from opensquilla.router_tiers import (
    DEFAULT_TEXT_TIER,
    HIGHEST_TEXT_TIER,
    ROUTER_TIER_ENSEMBLE_SELECTION_MODES,
    STATIC_B5_SELECTION_MODE_PROVIDERS,
    TEXT_TIERS,
    TierConfig,
    effective_ensemble_selection_mode,
    ensemble_selection_configured,
    normalize_text_tier,
    router_dynamic_tier_members_active,
    tier_provider_role,
)
from opensquilla.search.types import DEFAULT_SEARCH_MAX_RESULTS, MAX_SEARCH_RESULTS
from opensquilla.secrets import clean_header_secret

SearchFallbackPolicy = Literal["off", "network"]
RouterMode = Literal["recommended", "openrouter-mix", "custom", "disabled"]
RouterConflictAction = Literal[
    "preserve",
    "use_recommended",
    "enable_cross_provider",
    "disable",
]
_TEXT_ROUTER_TIERS = TEXT_TIERS
_ROUTER_TIER_KEYS = set(_TEXT_ROUTER_TIERS) | {"image_model"}
_TIER_KEY_ALIASES = {
    "thinkingLevel": "thinking_level",
    "supportsImage": "supports_image",
    "imageOnly": "image_only",
    "ensembleSelectionMode": "ensemble_selection_mode",
    "ensembleEnabled": "ensemble_enabled",
}
_REMOTE_MEMORY_EMBEDDING_PROVIDERS = {"openai", "openai-compatible"}
_DEFAULT_REMOTE_EMBEDDING_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_OLLAMA_EMBEDDING_BASE_URL = "http://localhost:11434"


@dataclass(frozen=True)
class MutationResult:
    config: GatewayConfig
    changed: bool
    restart_required: bool
    warnings: list[str] = field(default_factory=list)
    public_payload: dict[str, Any] = field(default_factory=dict)
    remove_paths: tuple[str, ...] = ()


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


class LlmProfileRemovalError(ValueError):
    """Stable, secret-free validation failure for atomic active-profile removal."""

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


def _clone(cfg: GatewayConfig) -> GatewayConfig:
    new_cfg = cfg.model_copy(deep=True)
    inherit_runtime_secrets(cfg, new_cfg)
    return new_cfg


def _clean_optional_str(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip()


def _ambient_llm_credential_available(config: GatewayConfig, *, spec_env_key: str) -> bool:
    """Return whether an external key source is active for the primary LLM."""

    configured_env = str(getattr(config.llm, "api_key_env", "") or "").strip()
    settings_env = environment_value("OPENSQUILLA_LLM_API_KEY_ENV").strip()
    for env_name in (configured_env, settings_env, str(spec_env_key or "").strip()):
        if env_name and environment_value(env_name):
            return True
    return bool(environment_value("OPENSQUILLA_LLM_API_KEY"))


def _positive_int(value: int | str, *, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be an integer >= 1") from None
    if parsed < 1:
        raise ValueError(f"{label} must be >= 1")
    return parsed


def _bounded_non_negative_int(
    value: int | str,
    *,
    label: str,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be an integer between 0 and {maximum}") from None
    if parsed < 0 or parsed > maximum:
        raise ValueError(f"{label} must be between 0 and {maximum}")
    return parsed


_C3_ENSEMBLE_POLICY_DEFAULTS: dict[str, object] = {
    "min_successful_proposers": 1,
    "proposer_max_retries": 1,
    "all_failed_policy": "fallback_single",
}


def _materialize_c3_ensemble_policy_defaults(config: GatewayConfig) -> tuple[str, ...]:
    """Persist C3's shared-fusion defaults without overriding operator values."""

    router = getattr(config, "squilla_router", None)
    tiers = getattr(router, "tiers", {}) or {}
    c3 = TierConfig.from_value(tiers.get(HIGHEST_TEXT_TIER))
    if not bool(getattr(router, "enabled", False)) or c3.ensemble_enabled is not True:
        return ()

    ensemble = config.llm_ensemble
    explicit_fields = set(getattr(ensemble, "model_fields_set", set()))
    generated_fields = {
        field_name
        for field_name in _C3_ENSEMBLE_POLICY_DEFAULTS
        if field_name not in explicit_fields
    }
    if not generated_fields:
        return ()

    payload = ensemble.model_dump(mode="python")
    for field_name in generated_fields:
        payload[field_name] = _C3_ENSEMBLE_POLICY_DEFAULTS[field_name]
    materialized = LlmEnsembleConfig(**payload)
    object.__setattr__(
        materialized,
        "__pydantic_fields_set__",
        explicit_fields | generated_fields,
    )
    config.llm_ensemble = materialized
    for field_name in generated_fields:
        config.mark_force_persist(f"llm_ensemble.{field_name}")
    return tuple(sorted(generated_fields))


def _preset_tiers_with_model(preset: ProviderPreset, model: str) -> dict[str, dict]:
    tiers = preset.tier_defaults()
    for tier in tiers.values():
        if not str(tier.get("model") or "").strip():
            tier["model"] = model
    return tiers


def _reconcile_router_profile_for_provider(
    cfg: GatewayConfig,
    provider_id: str,
    *,
    preset: ProviderPreset | None = None,
) -> None:
    router_enabled = bool(getattr(cfg.squilla_router, "enabled", True))
    preset = preset or get_preset(provider_id)
    if preset is None:
        raise ValueError(f"provider {provider_id!r} has no managed router preset")
    router_payload = cfg.squilla_router.model_dump(mode="python")
    router_payload.pop("tiers", None)
    router_payload["enabled"] = router_enabled
    router_payload["preset_binding"] = "follow_primary"
    if preset.persistable and router_enabled:
        router_payload["tier_profile"] = provider_id
    else:
        router_payload["tier_profile"] = None
        router_payload["tiers"] = _preset_tiers_with_model(
            preset,
            str(getattr(cfg.llm, "model", "") or "").strip(),
        )
    cfg.squilla_router = SquillaRouterConfig(**router_payload)


def _normalize_explicit_text_tier(default_tier: str | None) -> str | None:
    if default_tier is None:
        return None
    if not str(default_tier).strip():
        return None
    tier = normalize_text_tier(default_tier)
    if not tier:
        raise ValueError("defaultTier must reference a text tier")
    if tier not in _TEXT_ROUTER_TIERS:
        raise ValueError("defaultTier must reference a text tier")
    return tier


def _normalize_tier_payload(name: str, payload: Any) -> dict[str, Any]:
    if name not in _ROUTER_TIER_KEYS:
        raise ValueError(f"unknown router tier {name!r}")
    if not isinstance(payload, dict):
        raise ValueError(f"router tier {name!r} must be an object")
    out: dict[str, Any] = {}
    for key, value in payload.items():
        out[_TIER_KEY_ALIASES.get(str(key), str(key))] = value
    return out


def _enforce_router_tier_role_invariants(name: str, tier: dict[str, Any]) -> dict[str, Any]:
    if name != "image_model":
        return tier
    out = dict(tier)
    out["supports_image"] = True
    out["image_only"] = True
    return out


def _merge_router_tiers(
    base: dict[str, Any],
    overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = {name: dict(value) for name, value in base.items()}
    if not overrides:
        return merged
    if not isinstance(overrides, dict):
        raise ValueError("router tiers must be an object")
    for name, raw_override in overrides.items():
        tier_name = normalize_text_tier(name) or str(name)
        override = _normalize_tier_payload(tier_name, raw_override)
        current = dict(merged.get(tier_name, {}))
        # A pre-``ensemble_enabled`` client can still submit an explicit
        # per-tier selection mode.  That legacy field is an ownership
        # boundary: do not let a managed preset's new shared-plan flag turn
        # the explicit legacy profile into an inherited global selection.
        if (
            "ensemble_selection_mode" in override
            and "ensemble_enabled" not in override
        ):
            current.pop("ensemble_enabled", None)
        current.update(override)
        merged[tier_name] = _enforce_router_tier_role_invariants(tier_name, current)
    return merged


def _canonical_tier_value(tier: Mapping[str, Any]) -> dict[str, Any]:
    thinking = tier.get("thinking_level")
    if thinking is None:
        thinking = tier.get("thinkingLevel")
    raw_ensemble_enabled = tier.get(
        "ensemble_enabled",
        tier.get("ensembleEnabled"),
    )
    ensemble_enabled = (
        raw_ensemble_enabled if isinstance(raw_ensemble_enabled, bool) else None
    )
    legacy_selection_mode = str(
        tier.get("ensemble_selection_mode", tier.get("ensembleSelectionMode", "")) or ""
    ).strip()
    return {
        "provider": str(tier.get("provider") or "").strip().lower(),
        "model": str(tier.get("model") or "").strip(),
        "description": str(tier.get("description") or "").strip(),
        "thinking_level": (str(thinking or "").strip() or None),
        "supports_image": bool(tier.get("supports_image", tier.get("supportsImage", False))),
        "image_only": bool(tier.get("image_only", tier.get("imageOnly", False))),
        "ensemble_enabled": ensemble_enabled,
        # Once the new tri-state field exists it owns execution. Retained
        # legacy metadata is intentionally ignored for semantic preset
        # comparison, while still round-tripping in the actual tier dict.
        "ensemble_selection_mode": (
            legacy_selection_mode if ensemble_enabled is None else ""
        ),
    }


def _canonical_tier_map(tiers: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    if not isinstance(tiers, Mapping):
        return normalized
    for raw_name, raw_tier in tiers.items():
        name = normalize_text_tier(raw_name) or str(raw_name)
        if name not in _ROUTER_TIER_KEYS or not isinstance(raw_tier, Mapping):
            continue
        tier = _normalize_tier_payload(name, raw_tier)
        tier = _enforce_router_tier_role_invariants(name, tier)
        normalized[name] = _canonical_tier_value(tier)
    return normalized


def _tiers_equal_after_canonical_normalization(
    candidate: Mapping[str, Any] | None,
    preset_tiers: Mapping[str, Any],
) -> bool:
    return _canonical_tier_map(candidate) == _canonical_tier_map(preset_tiers)


def _router_tiers_hand_customized(
    config: GatewayConfig,
    *,
    explicit_model: str = "",
) -> bool:
    """Return whether an interactive save must preserve the inline ladder.

    Explicit ownership is authoritative.  Historical configs without a
    binding retain the previous shape comparison so upgrades do not turn a
    machine-seeded preset into a hand-authored ladder (or overwrite a real
    custom ladder).  This helper remains part of the onboarding-flow contract:
    the CLI uses it before offering to replace a ladder with a preset.
    """

    router = config.squilla_router
    binding = str(getattr(router, "preset_binding", "") or "").strip().lower()
    if binding == "custom":
        return True
    if binding == "follow_primary":
        return False
    if getattr(router, "tier_profile", None):
        return False
    tiers = getattr(router, "tiers", {}) or {}
    if not tiers:
        return False
    if _tiers_equal_after_canonical_normalization(tiers, _default_tiers()):
        return False
    provider = str(getattr(config.llm, "provider", "") or "").strip().lower()
    preset = get_preset(provider)
    if preset is not None:
        candidate_models = {
            str(getattr(config.llm, "model", "") or "").strip(),
            str(explicit_model or "").strip(),
        }
        for tier in tiers.values():
            if isinstance(tier, Mapping):
                candidate_models.add(str(tier.get("model") or "").strip())
        for candidate in sorted(candidate_models):
            seeded = _preset_tiers_with_model(preset, candidate)
            if _tiers_equal_after_canonical_normalization(tiers, seeded):
                return False
    return True


def _validate_router_tiers(tiers: dict[str, Any], default_tier: str) -> None:
    if default_tier not in _TEXT_ROUTER_TIERS:
        raise ValueError("defaultTier must reference a text tier")
    for tier_name in _TEXT_ROUTER_TIERS:
        tier = tiers.get(tier_name)
        if not isinstance(tier, dict):
            raise ValueError(f"router tier {tier_name!r} must be an object")
        if not str(tier.get("provider") or "").strip():
            raise ValueError(f"router tier {tier_name!r} requires provider")
        if not str(tier.get("model") or "").strip():
            raise ValueError(f"router tier {tier_name!r} requires model")
        selection_mode = TierConfig.from_value(tier).ensemble_selection_mode
        raw_ensemble_enabled = tier.get(
            "ensemble_enabled",
            tier.get("ensembleEnabled"),
        )
        if raw_ensemble_enabled is not None and not isinstance(raw_ensemble_enabled, bool):
            raise ValueError(f"router tier {tier_name!r} ensembleEnabled must be a boolean")
        if tier_name != HIGHEST_TEXT_TIER and raw_ensemble_enabled is True:
            raise ValueError(
                f"router tier {tier_name!r} ensembleEnabled is only supported "
                f"for {HIGHEST_TEXT_TIER}"
            )
        if selection_mode and selection_mode not in ROUTER_TIER_ENSEMBLE_SELECTION_MODES:
            allowed = ", ".join(sorted(ROUTER_TIER_ENSEMBLE_SELECTION_MODES))
            raise ValueError(
                f"router tier {tier_name!r} ensembleSelectionMode must be one of: {allowed}"
            )


def _tier_provider_deployment_unready_reason(
    provider_id: str,
    llm_profiles: dict[str, Any] | None,
) -> str | None:
    """Save-time readiness mirror of the runtime deployment resolver.

    Returns ``None`` when a routed tier for ``provider_id`` would resolve at
    turn time, else the resolver's failure reason. Delegating to
    ``resolve_provider_deployment`` (side-effect free without a pool
    acquirer) keeps config-time warnings from drifting against what routed
    turns actually resolve — including case-variant ``llm_profiles`` keys
    and the endpoint-origin gates on env-provided keys.
    """
    from opensquilla.provider.deployment import resolve_provider_deployment

    resolution = resolve_provider_deployment(
        SimpleNamespace(llm_profiles=dict(llm_profiles or {}), llm=None),
        provider_id,
        # Readiness only: any non-empty model id exercises the credential
        # and endpoint resolution paths.
        "credential-readiness-probe",
    )
    if resolution.ready:
        return None
    return resolution.reason or "deployment_unresolved"


def _cross_provider_tier_warnings(
    tiers: dict[str, Any],
    active_provider: str,
    *,
    shared_selection_mode: str = "",
    ensemble_globally_enabled: bool = False,
    cross_provider_enabled: bool = False,
    tier_provider_mismatch: str = "route",
    llm_profiles: dict[str, Any] | None = None,
) -> list[str]:
    """Warn about tiers naming a provider other than the active LLM provider.

    With cross-provider execution off, the warning mirrors the configured
    mismatch policy: ``veto`` stays on the active deployment, while legacy
    ``route`` runs the foreign model id against the active credentials. With
    cross-provider execution on, the tier executes on its own provider, so the
    check flips to credential resolvability (profile or env; secrets are never
    guessed).
    """
    if not active_provider:
        return []
    warnings: list[str] = []
    dynamic_members_active = router_dynamic_tier_members_active(
        tiers,
        shared_selection_mode=shared_selection_mode,
        ensemble_globally_enabled=ensemble_globally_enabled,
    )
    for tier_name in sorted(tiers):
        tier = tiers.get(tier_name)
        if not isinstance(tier, dict):
            continue
        provider_role = tier_provider_role(
            tier_name,
            tier,
            shared_selection_mode=shared_selection_mode,
            router_dynamic_members_active=dynamic_members_active,
            ensemble_globally_enabled=ensemble_globally_enabled,
        )
        if provider_role == "dormant_draft":
            continue
        if provider_role == "blocked":
            warnings.append(
                f"Router tier '{tier_name}' uses a shared multi-model plan that cannot "
                "be resolved. Choose a supported llm_ensemble.selection_mode before "
                "routing requests to this tier."
            )
            continue
        tier_provider = str(tier.get("provider") or "").strip().lower()
        if not tier_provider or tier_provider == active_provider:
            continue
        if not cross_provider_enabled:
            if str(tier_provider_mismatch or "route").strip().lower() == "veto":
                warnings.append(
                    f"Router tier '{tier_name}' names provider '{tier_provider}', but the "
                    f"active LLM provider is '{active_provider}'. Cross-provider routing is "
                    f"not enabled (squilla_router.cross_provider_tiers), so this tier's "
                    f"provider/model choice is vetoed and execution stays on the current "
                    f"'{active_provider}' deployment."
                )
            else:
                warnings.append(
                    f"Router tier '{tier_name}' names provider '{tier_provider}', but the "
                    f"active LLM provider is '{active_provider}'. Cross-provider routing is "
                    f"not enabled (squilla_router.cross_provider_tiers), so this tier's "
                    f"model will be requested from '{active_provider}'."
                )
        elif cross_provider_enabled:
            unready_reason = _tier_provider_deployment_unready_reason(
                tier_provider, llm_profiles
            )
            if unready_reason == "missing_base_url":
                warnings.append(
                    f"Router tier '{tier_name}' routes to provider '{tier_provider}' but no "
                    f"endpoint resolves for it. Add [llm_profiles.{tier_provider}] with "
                    f"base_url; until then the tier falls back to '{active_provider}'."
                )
            elif unready_reason is not None:
                warnings.append(
                    f"Router tier '{tier_name}' routes to provider '{tier_provider}' but no "
                    f"credentials resolve for it. Add [llm_profiles.{tier_provider}] with "
                    f"api_key or api_key_env, or export the provider's default env key; "
                    f"until then the tier falls back to '{active_provider}'."
                )
    return warnings


def _resolve_provider_preset(preset_id: str, provider_id: str) -> ProviderPreset | None:
    """Validate an explicitly requested preset against the target provider.

    Returns ``None`` when no preset was requested. A preset id that does not
    exist or that belongs to a different provider is a validation error —
    presets are provider-bound (packaged legacy ids and synthesized ids both
    equal their provider id).
    """
    preset_id_clean = _clean_optional_str(preset_id).lower()
    if not preset_id_clean:
        return None
    preset = get_preset(preset_id_clean)
    if preset is None or preset.provider_id != provider_id:
        raise ValueError(
            f"preset {preset_id!r} does not apply to provider {provider_id!r}"
        )
    return preset


def _normalize_router_conflict_action(value: str | None) -> RouterConflictAction:
    action = str(value or "preserve").strip().lower()
    allowed = {
        "preserve",
        "use_recommended",
        "enable_cross_provider",
        "disable",
    }
    if action not in allowed:
        raise LlmProfileActivationError(
            "invalid_router_action",
            "routerAction must be preserve, use_recommended, "
            "enable_cross_provider, or disable",
        )
    return cast(RouterConflictAction, action)


def _implicit_primary_and_router(config: GatewayConfig) -> bool:
    """True only for an untouched built-in primary/router pair.

    Missing ``preset_binding`` is legacy/unclassified and therefore custom by
    default.  The safe exceptions are a genuinely fresh config whose LLM
    provider and Router sections carry no authored fields at all, and that
    same pristine built-in pair after a runtime resolver or public-config
    round-trip has materialized its defaults as explicit fields.  The latter
    is recognized by value, conservatively: the primary must still be the
    credential-less built-in deployment with every provider setting at its
    default, and the inline Router ladder must still exactly match the
    built-in provider preset.  An explicit ``custom`` binding or any authored
    provider/ladder value therefore remains an ownership boundary.
    """

    llm_fields = set(getattr(config.llm, "model_fields_set", set()))
    router_fields = set(getattr(config.squilla_router, "model_fields_set", set()))
    if "provider" not in llm_fields and not router_fields:
        return True

    llm = config.llm
    llm_defaults = LlmProviderConfig.model_fields
    default_provider = str(llm_defaults["provider"].default or "").strip().lower()
    default_model = str(llm_defaults["model"].default or "").strip()
    default_base_url = str(llm_defaults["base_url"].default or "").strip()
    if (
        str(getattr(llm, "provider", "") or "").strip().lower() != default_provider
        or str(getattr(llm, "model", "") or "").strip() != default_model
        or str(getattr(llm, "api_key", "") or "").strip()
        or str(getattr(llm, "api_key_env", "") or "").strip()
        or str(getattr(llm, "base_url", "") or "").strip() != default_base_url
        or str(getattr(llm, "proxy", "") or "").strip()
        or int(getattr(llm, "max_tokens", 0) or 0) != 0
        or int(getattr(llm, "context_window_tokens", 0) or 0) != 0
        or getattr(llm, "temperature", None) is not None
        or getattr(llm, "top_p", None) is not None
        or getattr(llm, "thinking", None) is not None
        or int(getattr(llm, "provider_request_proof_max_chars", 0) or 0) != 0
        or bool(getattr(llm, "provider_routing", {}) or {})
    ):
        return False

    router = config.squilla_router
    if (
        getattr(router, "preset_binding", None) is not None
        or getattr(router, "tier_profile", None) is not None
    ):
        return False
    preset = get_preset(default_provider)
    return preset is not None and _tiers_equal_after_canonical_normalization(
        getattr(router, "tiers", {}) or {},
        preset.tier_defaults(),
    )


def _router_provider_conflicts(
    config: GatewayConfig,
    target_provider: str,
    *,
    shared_selection_mode: str | None = None,
) -> tuple[str, ...]:
    """Foreign providers that would be vetoed/misrouted after a primary swap."""

    router = config.squilla_router
    if not bool(getattr(router, "enabled", False)):
        return ()
    if bool(getattr(router, "cross_provider_tiers", False)):
        return ()
    target = str(target_provider or "").strip().lower()
    effective_shared_mode = (
        effective_ensemble_selection_mode(config)
        if shared_selection_mode is None
        else str(shared_selection_mode or "").strip()
    )
    tiers = getattr(router, "tiers", {}) or {}
    ensemble_globally_enabled = bool(
        getattr(getattr(config, "llm_ensemble", None), "enabled", False)
    )
    dynamic_members_active = router_dynamic_tier_members_active(
        tiers if isinstance(tiers, Mapping) else {},
        shared_selection_mode=effective_shared_mode,
        ensemble_globally_enabled=ensemble_globally_enabled,
    )
    conflicts: set[str] = set()
    if isinstance(tiers, Mapping):
        for tier_name, tier in tiers.items():
            if not isinstance(tier, Mapping):
                continue
            provider_role = tier_provider_role(
                tier_name,
                tier,
                shared_selection_mode=effective_shared_mode,
                router_dynamic_members_active=dynamic_members_active,
                ensemble_globally_enabled=ensemble_globally_enabled,
            )
            if provider_role not in {"direct", "dynamic_member"}:
                continue
            provider = str(tier.get("provider") or "").strip().lower()
            if provider and provider != target:
                conflicts.add(provider)
    return tuple(sorted(conflicts))


def _preserve_router_as_custom(
    cfg: GatewayConfig,
    *,
    enabled: bool | None = None,
    enable_cross_provider: bool = False,
) -> None:
    """Materialize the effective ladder and mark explicit operator ownership."""

    router_payload = cfg.squilla_router.model_dump(mode="python")
    router_payload["tier_profile"] = None
    router_payload["preset_binding"] = "custom"
    router_payload["tiers"] = {
        name: (dict(tier) if isinstance(tier, Mapping) else tier)
        for name, tier in (getattr(cfg.squilla_router, "tiers", {}) or {}).items()
    }
    if enabled is not None:
        router_payload["enabled"] = enabled
    if enable_cross_provider:
        router_payload["cross_provider_tiers"] = True
    cfg.squilla_router = SquillaRouterConfig(**router_payload)


def _apply_primary_provider_router_policy(
    source: GatewayConfig,
    candidate: GatewayConfig,
    *,
    target_provider: str,
    router_action: str | None = None,
    explicit_preset: ProviderPreset | None = None,
) -> None:
    """Apply the single Router contract for every primary-provider switch.

    Managed ladders follow the target provider while retaining Router enabled
    state and all orthogonal settings.  Explicit custom and unclassified
    legacy ladders are byte-preserved unless the caller selects one of the
    conflict-resolution actions.  Ensemble state is intentionally outside
    this helper and is never touched.
    """

    action = _normalize_router_conflict_action(router_action)
    binding = getattr(source.squilla_router, "preset_binding", None)
    source_provider = str(getattr(source.llm, "provider", "") or "").strip().lower()
    target = str(target_provider or "").strip().lower()
    primary_changed = source_provider != target

    if action == "use_recommended":
        _reconcile_router_profile_for_provider(candidate, target_provider)
        return

    if action == "enable_cross_provider":
        _preserve_router_as_custom(candidate, enable_cross_provider=True)
        return

    if action == "disable":
        _preserve_router_as_custom(candidate, enabled=False)
        return

    if explicit_preset is not None:
        _reconcile_router_profile_for_provider(
            candidate,
            target_provider,
            preset=explicit_preset,
        )
        return

    if binding == "follow_primary" or (
        binding is None and _implicit_primary_and_router(source)
    ):
        _reconcile_router_profile_for_provider(candidate, target_provider)
        return

    # A credential rotation or direct-model edit is not a provider switch.
    # Preserve operator-owned/legacy ladders even when they intentionally
    # contain foreign tiers with cross-provider execution disabled; the save
    # neither introduces nor worsens that pre-existing state.
    if not primary_changed:
        return

    conflicts = _router_provider_conflicts(
        source,
        target_provider,
        shared_selection_mode=effective_ensemble_selection_mode(candidate),
    )
    if conflicts:
        joined = ", ".join(conflicts)
        raise LlmProfileActivationError(
            "router_provider_conflict",
            "custom Router tiers reference provider(s) that differ from the "
            f"new primary: {joined}",
            details={
                "conflictProviders": list(conflicts),
                "allowedRouterActions": [
                    "use_recommended",
                    "enable_cross_provider",
                    "disable",
                ],
            },
        )


def upsert_llm_provider(
    config: GatewayConfig,
    *,
    provider_id: str,
    model: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    preserve_api_key: bool = False,
    base_url: str | None = None,
    proxy: str | None = None,
    provider_routing: dict[str, str] | None = None,
    preset_id: str | None = None,
    router_action: str | None = None,
    image_generation_intent: str | None = None,
) -> MutationResult:
    """Save the active LLM provider configuration.

    Keep-current contract (``None`` = "not passed"): when re-saving the
    provider that is already active (``config.llm.provider == provider_id``,
    e.g. a key rotation), every optional field left at ``None`` keeps its
    stored value — ``model``, ``base_url``, ``proxy``, ``provider_routing``,
    plus parameterless fields such as ``max_tokens`` and ``thinking``, which
    are always carried over verbatim on a same-provider re-save. Explicit
    values always win, and an explicit empty string keeps its legacy
    meaning: ``model=""``/``base_url=""`` fall back to derived defaults
    (preset/tier model, spec base URL), ``proxy=""`` clears the proxy, and
    optional-provider ``api_key=""`` clears the stored key unless the caller
    explicitly sets ``preserve_api_key=True``. Required providers keep a
    blank credential only while the endpoint origin is unchanged; a changed
    scheme/host/effective port drops every reusable credential source
    fail-closed so the operator re-enters it. On a provider switch nothing is
    carried over except the caller's values; credentials never follow across
    providers or endpoint origins.

    Router ownership is explicit and shared with profile activation:
    ``follow_primary`` reconciles to this provider while preserving Router
    enabled state and orthogonal settings; ``custom`` and legacy/unclassified
    ladders are preserved.  A cross-provider custom ladder that cannot execute
    with cross-provider routing off is rejected unless ``router_action``
    resolves it explicitly.

    ``image_generation_intent`` is an additive client contract. Its default
    is ``preserve`` for old clients; ``enable_provider_default`` may add the
    OpenRouter ``follow_llm`` route only when no image settings are owned.
    """
    spec = get_provider_setup_spec(provider_id)
    if not spec.runtime_supported:
        raise ValueError(
            f"provider {provider_id!r} is not runtime-supported and cannot be configured"
        )
    if is_redacted_secret_sentinel(api_key):
        # A round-tripped redaction mask ('***' or any all-asterisk echo)
        # means "keep the stored key", never a literal credential. Enforced
        # here, server-side, so every RPC/CLI client gets the same trust
        # boundary the channel-secret merge already provides.
        api_key = None
        preserve_api_key = True
    api_key = api_key or ""
    api_key_env = api_key_env or ""
    preset = _resolve_provider_preset(preset_id or "", provider_id)
    same_provider = config.llm.provider == provider_id
    model_clean = _clean_optional_str(model)
    if not model_clean and model is None and same_provider:
        # Not passed at all: keep the stored model on a same-provider
        # re-save instead of resetting it to a derived default.
        model_clean = str(config.llm.model or "").strip()
    if not model_clean and preset is not None:
        # Explicit preset application: the preset's default model fills the
        # provider's direct model when the caller gave none.
        model_clean = preset.default_model.strip()
    if not model_clean:
        # The primary model is the provider's direct/fallback deployment. It
        # must not track SquillaRouter's independently selectable default
        # tier: changing the route only changes routed turns, while direct
        # mode and fail-closed fallback keep using this provider default.
        model_clean = str(spec.default_direct_model or "").strip()
    if not model_clean:
        raise ValueError("model is required")
    effective_base_url = base_url or ""
    if not effective_base_url and base_url is None and same_provider:
        effective_base_url = str(config.llm.base_url or "")
    if not effective_base_url:
        effective_base_url = spec.default_base_url
    # Compare the *derived* endpoints: an empty stored base URL means the
    # provider default, so it must match the same derived default and let a
    # same-endpoint key rotation still reuse the stored secret. Only a real
    # scheme/host/effective-port change blocks reuse.
    stored_endpoint = str(config.llm.base_url or "") or spec.default_base_url
    stored_credentials_match_endpoint = base_url_allows_credential_reuse(
        stored_endpoint,
        effective_base_url,
    )
    # Blank credential fields keep the stored key on a same-provider re-save,
    # but a stored secret never follows a changed endpoint origin: required
    # and optional providers alike drop every reusable credential source when
    # the final base URL is a different scheme/host/effective port, so the
    # operator must re-enter it for the new endpoint. This keeps legacy
    # api_key="" clearing and same-origin key rotation intact while giving
    # newer clients an unambiguous password-field "leave current key
    # unchanged" affordance without carrying secrets to another configurable
    # endpoint.
    effective_api_key = clean_header_secret(api_key, label="LLM API key")
    if api_key and api_key_env.strip():
        raise ValueError("configure either api_key or api_key_env, not both")
    effective_api_key_env = "" if api_key else api_key_env.strip()
    if (
        effective_api_key_env
        and same_provider
        and not stored_credentials_match_endpoint
        and effective_api_key_env == getattr(config.llm, "api_key_env", "").strip()
    ):
        # Clients hydrate and re-send the stored env-var name verbatim: a
        # re-submitted value equal to the stored reference means "keep the
        # current credential", not a credential authored for the changed
        # endpoint origin, so it is gated like every stored source.
        effective_api_key_env = ""
    if (
        not api_key
        and not effective_api_key_env
        and same_provider
        and stored_credentials_match_endpoint
        and (spec.requires_api_key or spec.accepts_api_key)
    ):
        effective_api_key_env = getattr(config.llm, "api_key_env", "").strip()
    if (
        same_provider
        and not stored_credentials_match_endpoint
        and spec.accepts_api_key
        and not spec.requires_api_key
        and not effective_api_key
        and not effective_api_key_env
        and _ambient_llm_credential_available(config, spec_env_key=spec.env_key)
    ):
        # Optional/keyless providers may otherwise accept the endpoint change,
        # clear the stored reference, and immediately recover the same secret
        # through a registry or OPENSQUILLA_LLM_* fallback. Require a newly
        # authored credential while that ambient source is active; existing
        # custom-provider configs remain untouched until an origin is changed.
        raise ValueError(
            "changing provider endpoint origin while an ambient credential is active "
            "requires a new api_key or api_key_env"
        )
    stored_api_key_is_explicit = bool(config.llm.api_key) and (
        "llm.api_key" not in getattr(config, "_runtime_secret_paths", set())
    )
    preserve_optional_api_key = (
        preserve_api_key
        and spec.accepts_api_key
        and stored_api_key_is_explicit
        and stored_credentials_match_endpoint
    )
    if (
        not effective_api_key
        and not api_key_env.strip()
        and same_provider
        and config.llm.api_key
        and (
            (spec.requires_api_key and stored_credentials_match_endpoint)
            or preserve_optional_api_key
        )
    ):
        effective_api_key = config.llm.api_key
    if spec.requires_api_key and not effective_api_key and not effective_api_key_env:
        raise ValueError(f"provider {provider_id!r} requires an api_key")
    if spec.requires_base_url and not effective_base_url:
        raise ValueError(f"provider {provider_id!r} requires a base_url")
    if proxy is None:
        effective_proxy = str(config.llm.proxy or "") if same_provider else ""
    else:
        effective_proxy = proxy
    if provider_routing is None:
        effective_provider_routing = (
            dict(config.llm.provider_routing or {}) if same_provider else {}
        )
    else:
        effective_provider_routing = dict(provider_routing)

    new_cfg = _clone(config)
    # Seed from the stored section on a same-provider re-save so fields
    # without a parameter here (max_tokens, thinking, future additions) keep
    # their values instead of silently resetting to model defaults.
    llm_payload: dict[str, Any] = (
        config.llm.model_dump(mode="python") if same_provider else {}
    )
    llm_payload.update(
        {
            "provider": provider_id,
            "model": model_clean,
            "api_key": effective_api_key,
            "api_key_env": effective_api_key_env,
            "base_url": effective_base_url,
            "proxy": effective_proxy,
            "provider_routing": effective_provider_routing,
        }
    )
    new_cfg.llm = LlmProviderConfig(**llm_payload)
    # ``provider_id`` is a required argument on this mutation, so it is an
    # explicit operator identity decision even when it equals the built-in
    # default. Sparse persistence must not erase that provenance: a
    # provider-less file carrying only a key is intentionally interpreted as
    # legacy OpenRouter on reload.
    new_cfg.mark_force_persist("llm.provider")
    new_cfg.clear_runtime_override("llm.api_key")
    new_cfg.set_provider_resolution(
        status="explicit",
        effective_provider=provider_id,
        source="operator",
        reason_code="provider_explicit",
    )
    _apply_primary_provider_router_policy(
        config,
        new_cfg,
        target_provider=provider_id,
        router_action=router_action,
        explicit_preset=preset,
    )
    _materialize_c3_ensemble_policy_defaults(new_cfg)
    if api_key:
        clear_runtime_secret_paths(new_cfg, {"llm.api_key"})
    # Explicit endpoint/proxy values override any boot-time env resolution:
    # drop the runtime-override record so the persist layer writes exactly
    # what the operator passed even when it equals the env value. Only a
    # genuinely explicit NON-EMPTY value counts: ``None`` is keep-current and
    # the empty string is the legacy RPC reset sentinel ("derive default"),
    # so neither names an operator endpoint — clearing the record for them
    # would let a boot-time env value get baked into config.toml by a plain
    # re-save (e.g. a WebUI key rotation that sends baseUrl="").
    if base_url:
        new_cfg.clear_runtime_override("llm.base_url")
    if proxy:
        new_cfg.clear_runtime_override("llm.proxy")

    image_generation_change = apply_image_generation_intent(
        config,
        new_cfg,
        provider_id=provider_id,
        intent=image_generation_intent,
    )

    payload: dict[str, Any] = {
        "provider": provider_id,
        "model": model_clean,
        "api_key": effective_api_key,
        "api_key_env": effective_api_key_env,
        "api_key_source": (
            "explicit" if effective_api_key else ("env" if effective_api_key_env else "none")
        ),
        "base_url": effective_base_url,
        "proxy": effective_proxy,
        "provider_routing": effective_provider_routing,
    }
    if image_generation_change is not None:
        payload["capabilityChanges"] = {
            "imageGeneration": image_generation_change,
        }
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=False,
        warnings=[],
        public_payload=redact_provider_payload(payload),
    )


def clear_llm_provider_credentials(
    config: GatewayConfig,
    *,
    provider_id: str,
) -> MutationResult:
    """Remove every stored credential source from the active LLM provider.

    This is deliberately separate from :func:`upsert_llm_provider`: blank
    credential fields on that compatibility surface retain their historical
    keep-current behavior for providers that require a key.  Clearing a
    credential must therefore be an explicit operation that cannot be
    confused with an omitted password field.

    Provider identity, model, endpoint, proxy, request tuning, Router and
    Ensemble configuration are copied verbatim.  The caller remains
    responsible for persisting the returned candidate before hot-applying it.
    """
    provider = str(provider_id or "").strip().lower()
    active_provider = str(config.llm.provider or "").strip().lower()
    if not provider:
        raise ValueError("providerId is required")
    if provider != active_provider:
        raise ValueError(
            f"credential clear only supports the active provider {active_provider!r}"
        )

    new_cfg = _clone(config)
    old_api_key = str(getattr(new_cfg.llm, "api_key", "") or "")
    old_api_key_env = str(getattr(new_cfg.llm, "api_key_env", "") or "")
    runtime_secret_paths: set[str] = getattr(new_cfg, "_runtime_secret_paths", set())
    explicit_secret_paths: set[str] = getattr(new_cfg, "_explicit_secret_paths", set())
    had_provenance = (
        "llm.api_key" in runtime_secret_paths
        or "llm.api_key" in explicit_secret_paths
    )
    new_cfg.llm.api_key = ""
    new_cfg.llm.api_key_env = ""
    # A runtime-resolved value can still be materialized in llm.api_key even
    # though it was never persisted.  Clearing removes both that cached value
    # and its provenance.  If the provider's default environment variable is
    # still exported, runtime resolution may use it again; the RPC response
    # reports that external source explicitly.
    new_cfg._runtime_secret_paths.discard("llm.api_key")
    new_cfg._explicit_secret_paths.discard("llm.api_key")

    return MutationResult(
        config=new_cfg,
        changed=bool(old_api_key or old_api_key_env or had_provenance),
        restart_required=False,
        public_payload={
            "provider": provider,
            "active": True,
            "storedCredentialsCleared": True,
        },
    )


def upsert_router(
    config: GatewayConfig,
    *,
    mode: str = "recommended",
    default_tier: str | None = None,
    tiers: dict[str, Any] | None = None,
    cross_provider_tiers: bool | None = None,
    tier_provider_mismatch: str | None = None,
) -> MutationResult:
    if mode not in {"recommended", "openrouter-mix", "custom", "disabled"}:
        raise ValueError(
            "router mode must be recommended, openrouter-mix, custom, or disabled"
        )
    router_mode = cast(RouterMode, mode)
    provider = str(config.llm.provider or "").strip().lower()
    router_payload = config.squilla_router.model_dump(mode="python")
    router_payload.pop("tiers", None)
    if cross_provider_tiers is not None:
        router_payload["cross_provider_tiers"] = bool(cross_provider_tiers)
    if tier_provider_mismatch is not None:
        mismatch_policy = str(tier_provider_mismatch or "").strip()
        if mismatch_policy not in {"route", "veto"}:
            raise ValueError("tierProviderMismatch must be route or veto")
        router_payload["tier_provider_mismatch"] = mismatch_policy

    default_tier_override = _normalize_explicit_text_tier(default_tier)
    default_tier_clean = default_tier_override or str(
        normalize_text_tier(router_payload.get("default_tier")) or DEFAULT_TEXT_TIER
    )
    if default_tier_override is not None:
        router_payload["default_tier"] = default_tier_clean

    public_payload: dict[str, Any] = {}
    if router_mode == "disabled":
        router_payload["enabled"] = False
        router_payload["tier_profile"] = None
        # Keep the effective ladder stored inline while disabled so a later
        # re-enable can restore an operator-authored tier ladder instead of
        # silently resetting it to the packaged defaults.
        router_payload["tiers"] = {
            name: (dict(tier) if isinstance(tier, dict) else tier)
            for name, tier in (getattr(config.squilla_router, "tiers", {}) or {}).items()
        }
        public_payload["mode"] = "disabled"
        public_payload.update({"enabled": False, "tier_profile": None})
    else:
        preset = get_preset(provider)
        active_model = str(getattr(config.llm, "model", "") or "").strip()
        base_tiers = _preset_tiers_with_model(preset, active_model) if preset is not None else {}
        source_tiers = tiers
        if router_mode == "openrouter-mix":
            if provider != "openrouter":
                raise ValueError(
                    "openrouter-mix router mode is only valid for openrouter LLM provider"
                )
            source_tiers = (
                tiers if tiers is not None else getattr(config.squilla_router, "tiers", {})
            )
        elif router_mode == "custom" and tiers is None:
            # No tiers passed: an inline (possibly hand-edited) ladder —
            # including one preserved across a disable — is the effective
            # state, so keep it rather than resetting to the preset base.
            stored_tiers = getattr(config.squilla_router, "tiers", {}) or {}
            if (
                getattr(config.squilla_router, "tier_profile", None) is None
                and stored_tiers
                and not _tiers_equal_after_canonical_normalization(
                    stored_tiers, _default_tiers()
                )
            ):
                source_tiers = stored_tiers
        merged_tiers = _merge_router_tiers(base_tiers, source_tiers)
        if preset is None:
            # A hand-edited, non-registry llm.provider has no packaged
            # profile to seed tiers from; unless the caller supplied a full
            # ladder, the advertised router one-liner would otherwise die on
            # the cryptic "router tier 'c0' must be an object".
            missing_tiers = [
                tier_name
                for tier_name in _TEXT_ROUTER_TIERS
                if not isinstance(merged_tiers.get(tier_name), dict)
            ]
            if missing_tiers:
                raise ValueError(
                    f"llm.provider {provider!r} is not a registered provider, so no "
                    f"packaged router profile can seed tiers "
                    f"{', '.join(missing_tiers)}. Configure a registered provider "
                    f"first (opensquilla onboard configure provider --provider "
                    f"<id>) or disable the router (opensquilla onboard configure "
                    f"router --router disabled)."
                )
        follows_managed_preset = (
            router_mode in {"recommended", "openrouter-mix"}
            and preset is not None
            and _tiers_equal_after_canonical_normalization(merged_tiers, base_tiers)
        )
        router_payload["preset_binding"] = (
            "follow_primary" if follows_managed_preset else "custom"
        )
        writes_packaged_profile = (
            follows_managed_preset and preset is not None and preset.persistable
        )
        if writes_packaged_profile:
            router_payload["enabled"] = True
            router_payload["tier_profile"] = provider
            router_payload["tiers"] = merged_tiers
            public_payload["mode"] = "recommended"
            public_payload.update({"enabled": True, "tier_profile": provider})
        else:
            router_payload["enabled"] = True
            router_payload["tier_profile"] = None
            router_payload["tiers"] = merged_tiers
            public_payload["mode"] = "recommended" if follows_managed_preset else "custom"
            public_payload.update({"enabled": True, "tier_profile": None})
    warnings: list[str] = []
    if router_payload.get("enabled"):
        _validate_router_tiers(
            cast(dict[str, Any], router_payload.get("tiers") or {}),
            default_tier_clean,
        )
        warnings = _cross_provider_tier_warnings(
            cast(dict[str, Any], router_payload.get("tiers") or {}),
            provider,
            shared_selection_mode=effective_ensemble_selection_mode(config),
            ensemble_globally_enabled=bool(
                getattr(getattr(config, "llm_ensemble", None), "enabled", False)
            ),
            cross_provider_enabled=bool(router_payload.get("cross_provider_tiers")),
            tier_provider_mismatch=str(
                router_payload.get("tier_provider_mismatch") or "route"
            ),
            llm_profiles=getattr(config, "llm_profiles", None),
        )

    new_cfg = _clone(config)
    new_cfg.squilla_router = SquillaRouterConfig(**router_payload)
    if router_mode == "disabled":
        apply_model_routing_mode(new_cfg, "direct")
    elif not bool(getattr(config.squilla_router, "enabled", False)):
        # A genuine enable is a strategy switch: route through the canonical
        # mode patch (ensemble off, rollout_phase full, force-persisted).
        apply_model_routing_mode(new_cfg, "router")
    shared_tier_enabled = (
        bool(getattr(new_cfg.squilla_router, "enabled", False))
        and TierConfig.from_value(
            (getattr(new_cfg.squilla_router, "tiers", {}) or {}).get(HIGHEST_TEXT_TIER)
        ).ensemble_enabled
        is True
    )
    if shared_tier_enabled and not ensemble_selection_configured(new_cfg):
        activation = ensemble_activation_patches(new_cfg)
        if activation:
            ensemble_payload = new_cfg.llm_ensemble.model_dump(mode="python")
            generated_fields: set[str] = set()
            if "llm_ensemble.selection_mode" in activation:
                ensemble_payload["selection_mode"] = activation[
                    "llm_ensemble.selection_mode"
                ]
                generated_fields.add("selection_mode")
            if "llm_ensemble.candidates" in activation:
                ensemble_payload["candidates"] = activation[
                    "llm_ensemble.candidates"
                ]
                generated_fields.add("candidates")
            materialized = LlmEnsembleConfig(**ensemble_payload)
            fields_set = set(
                getattr(new_cfg.llm_ensemble, "model_fields_set", set())
            ) | generated_fields
            object.__setattr__(materialized, "__pydantic_fields_set__", fields_set)
            new_cfg.llm_ensemble = materialized
            for field_name in generated_fields:
                new_cfg.mark_force_persist(f"llm_ensemble.{field_name}")
    if shared_tier_enabled:
        _materialize_c3_ensemble_policy_defaults(new_cfg)
    # Otherwise this is ladder/settings maintenance on an already-enabled
    # router (the common Web UI tier-table save and CLI default-tier path).
    # Applying the mode patch here would silently escalate an operator's
    # rollout_phase='observe'/'prompt_only' to live 'full' routing and turn
    # off a running ensemble, so the stored strategy fields are preserved.
    public_payload["default_tier"] = new_cfg.squilla_router.default_tier
    public_payload["tiers"] = redact_router_tiers_payload(new_cfg.squilla_router.tiers)
    public_payload["cross_provider_tiers"] = bool(new_cfg.squilla_router.cross_provider_tiers)
    public_payload["tier_provider_mismatch"] = new_cfg.squilla_router.tier_provider_mismatch
    public_payload["router_binding"] = (
        new_cfg.squilla_router.preset_binding or "legacy"
    )
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=False,
        warnings=warnings,
        public_payload=public_payload,
    )


# Values the RPC surface may write into [llm_ensemble]. Sourced from the
# config model's own Literal annotations so the mutation can never drift
# from what GatewayConfig actually accepts.
_LLM_ENSEMBLE_SELECTION_MODES: tuple[str, ...] = tuple(
    str(value)
    for value in get_args(LlmEnsembleConfig.model_fields["selection_mode"].annotation)
)
_LLM_ENSEMBLE_ALL_FAILED_POLICIES: tuple[str, ...] = tuple(
    str(value)
    for value in get_args(LlmEnsembleConfig.model_fields["all_failed_policy"].annotation)
)


def upsert_llm_ensemble(
    config: GatewayConfig,
    *,
    enabled: bool | None = None,
    selection_mode: str | None = None,
    model_options: list[str] | None = None,
    candidates: list[dict[str, object]] | None = None,
    min_successful_proposers: int | str | None = None,
    proposer_max_retries: int | str | None = None,
    all_failed_policy: str | None = None,
) -> MutationResult:
    """Update the ``[llm_ensemble]`` routing surface.

    Partial-payload semantics are pinned: the merge seeds from the *current*
    ``llm_ensemble`` section and overrides only the keys explicitly present
    in the request (``None`` = keep current). Omitted keys must never reset
    to defaults — an enabled-only save from a client must not clobber an
    operator's explicit ``selection_mode`` or ``model_options``.

    The TurnRunner reads ``llm_ensemble`` live from the running config, so
    no restart is required.
    """
    current = config.llm_ensemble.model_dump(mode="python")
    merged = dict(current)
    explicit_fields = set(
        getattr(config.llm_ensemble, "model_fields_set", set())
    )
    generated_fields: set[str] = set()

    if enabled is not None:
        merged["enabled"] = bool(enabled)
        explicit_fields.add("enabled")
    if selection_mode is not None:
        mode_clean = str(selection_mode).strip()
        if mode_clean not in _LLM_ENSEMBLE_SELECTION_MODES:
            raise ValueError(
                "selection_mode must be one of: "
                + ", ".join(_LLM_ENSEMBLE_SELECTION_MODES)
            )
        merged["selection_mode"] = mode_clean
        explicit_fields.add("selection_mode")
    if model_options is not None:
        if not isinstance(model_options, (list, tuple)):
            raise ValueError("model_options must be a list of model ids")
        merged["model_options"] = [str(option) for option in model_options]
        explicit_fields.add("model_options")
    if candidates is not None:
        if not isinstance(candidates, (list, tuple)):
            raise ValueError("candidates must be a list of candidate objects")
        # Merge incoming candidate payloads with stored rows keyed by
        # (provider, model).  Clients (console UI, desktop) may only send a
        # subset of fields; keys absent from the payload are preserved from
        # the stored row so server-side-only fields (e.g. thinking_level)
        # survive a UI save.  Candidates not present in the payload are
        # treated as deleted (the UI always sends the full visible list).
        stored_by_key: dict[tuple[str, str], dict[str, object]] = {}
        for stored in merged.get("candidates", []) or []:
            if isinstance(stored, dict):
                key = (
                    str(stored.get("provider", "") or "").strip().lower(),
                    str(stored.get("model", "") or "").strip(),
                )
                stored_by_key[key] = dict(stored)
        candidate_payloads: list[dict[str, object]] = []
        for entry in candidates:
            if not isinstance(entry, dict):
                raise ValueError("candidates must be a list of candidate objects")
            incoming = dict(entry)
            key = (
                str(incoming.get("provider", "") or "").strip().lower(),
                str(incoming.get("model", "") or "").strip(),
            )
            stored_row = stored_by_key.get(key)
            if stored_row is not None:
                # Stored fields fill gaps; incoming fields take precedence.
                merged_row = {**stored_row, **incoming}
            else:
                merged_row = incoming
            candidate_payloads.append(merged_row)
        merged["candidates"] = candidate_payloads
        explicit_fields.add("candidates")
    if min_successful_proposers is not None:
        merged["min_successful_proposers"] = _positive_int(
            min_successful_proposers, label="min_successful_proposers"
        )
        explicit_fields.add("min_successful_proposers")
    if proposer_max_retries is not None:
        merged["proposer_max_retries"] = _bounded_non_negative_int(
            proposer_max_retries,
            label="proposer_max_retries",
            maximum=10,
        )
        explicit_fields.add("proposer_max_retries")
    if all_failed_policy is not None:
        policy_clean = str(all_failed_policy).strip()
        if policy_clean not in _LLM_ENSEMBLE_ALL_FAILED_POLICIES:
            raise ValueError(
                "all_failed_policy must be one of: "
                + ", ".join(_LLM_ENSEMBLE_ALL_FAILED_POLICIES)
            )
        merged["all_failed_policy"] = policy_clean
        explicit_fields.add("all_failed_policy")

    if (
        enabled is True
        and not bool(current.get("enabled", False))
        and selection_mode is None
    ):
        activation = ensemble_activation_patches(config)
        if activation:
            merged["selection_mode"] = activation["llm_ensemble.selection_mode"]
            generated_fields.add("selection_mode")
            if "llm_ensemble.candidates" in activation:
                merged["candidates"] = activation["llm_ensemble.candidates"]
                generated_fields.add("candidates")
            explicit_fields.update(generated_fields)

    try:
        new_ensemble = LlmEnsembleConfig(**merged)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc

    new_cfg = _clone(config)
    object.__setattr__(
        new_ensemble,
        "__pydantic_fields_set__",
        explicit_fields,
    )
    new_cfg.llm_ensemble = new_ensemble
    routing_changes: dict[str, Any] = {}
    enabled_changed = enabled is not None and bool(enabled) != bool(
        current.get("enabled", False)
    )
    if enabled_changed:
        routing_changes = apply_model_routing_mode(
            new_cfg,
            "ensemble" if new_ensemble.enabled else "direct",
        )
    elif selection_mode is not None and new_ensemble.enabled:
        # Changing a live Ensemble implementation can change whether it owns
        # an internal Router dependency (dynamic/unknown vs static/custom),
        # without clobbering an advanced prompt_only rollout phase.
        routing_changes = reconcile_model_routing_write(
            new_cfg,
            {"llm_ensemble.selection_mode"},
        )
    # A value-identical ``enabled`` re-assertion (e.g. `configure ensemble
    # --disabled` run twice, or a settings form that always sends the flag)
    # is not a mode selection: reapplying the mode patch would disable an
    # active Router and reset an advanced rollout_phase to its derived value.
    if enabled is not None:
        # An explicit enabled/disabled decision must be visible in the file
        # even when it equals the model default — otherwise a headless
        # `configure ensemble --disabled` on a fresh config persists nothing
        # and is indistinguishable from a silent no-op.
        new_cfg.mark_force_persist("llm_ensemble.enabled")
    if selection_mode is not None or "selection_mode" in generated_fields:
        new_cfg.mark_force_persist("llm_ensemble.selection_mode")
    if candidates is not None or "candidates" in generated_fields:
        new_cfg.mark_force_persist("llm_ensemble.candidates")
    if all_failed_policy is not None:
        new_cfg.mark_force_persist("llm_ensemble.all_failed_policy")
    if proposer_max_retries is not None:
        new_cfg.mark_force_persist("llm_ensemble.proposer_max_retries")

    payload: dict[str, Any] = {
        "enabled": new_ensemble.enabled,
        "selection_mode": new_ensemble.selection_mode,
        "model_options": list(new_ensemble.model_options),
        "min_successful_proposers": new_ensemble.min_successful_proposers,
        "proposer_max_retries": new_ensemble.proposer_max_retries,
        "all_failed_policy": new_ensemble.all_failed_policy,
    }
    if candidates is not None or new_ensemble.candidates:
        payload["candidates"] = [
            candidate.model_dump(mode="python")
            for candidate in new_ensemble.candidates
        ]
    return MutationResult(
        config=new_cfg,
        changed=(
            current != new_ensemble.model_dump(mode="python")
            or bool(routing_changes)
        ),
        restart_required=False,
        warnings=[],
        public_payload=payload,
    )


def upsert_search_provider(
    config: GatewayConfig,
    *,
    provider_id: str,
    api_key: str | None = None,
    api_key_env: str | None = None,
    max_results: int | str | None = None,
    proxy: str | None = None,
    use_env_proxy: bool | None = None,
    fallback_policy: str | None = None,
    diagnostics: bool | None = None,
) -> MutationResult:
    """Save the web search provider configuration.

    Keep-current contract (``None`` = "not passed"): ``max_results``,
    ``proxy``, ``use_env_proxy``, ``fallback_policy``, and ``diagnostics``
    keep their currently stored values when omitted — these are global
    search settings, so keep-current applies even when ``provider_id``
    changes. Explicit values always win (``proxy=""`` clears the proxy).
    A blank ``api_key`` keeps the stored key when re-saving the provider
    that is already active.
    """
    spec = get_search_provider_setup_spec(provider_id)
    if not spec.runtime_supported:
        raise ValueError(
            f"search provider {provider_id!r} is not runtime-supported and cannot be configured"
        )
    if is_redacted_secret_sentinel(api_key):
        # Round-tripped redaction mask: keep the stored key (see
        # upsert_llm_provider for the server-side trust-boundary rationale).
        api_key = None
    api_key = api_key or ""
    api_key_env = api_key_env or ""
    if max_results is None:
        effective_max_results = int(config.search_max_results)
    else:
        # Cap the write side to the same ceiling the config field enforces so
        # an over-range request is clamped here with a clear path rather than
        # failing late with a raw validation error at persist time.
        effective_max_results = min(
            _positive_int(max_results, label="max_results"), MAX_SEARCH_RESULTS
        )
    if fallback_policy is None:
        fallback_policy_value = cast(SearchFallbackPolicy, config.search_fallback_policy)
    else:
        if fallback_policy not in {"off", "network"}:
            raise ValueError("fallback_policy must be 'off' or 'network'")
        fallback_policy_value = cast(SearchFallbackPolicy, fallback_policy)
    effective_proxy = str(config.search_proxy or "") if proxy is None else proxy
    effective_use_env_proxy = (
        bool(config.search_use_env_proxy) if use_env_proxy is None else bool(use_env_proxy)
    )
    effective_diagnostics = (
        bool(config.search_diagnostics) if diagnostics is None else bool(diagnostics)
    )

    effective_api_key = (
        clean_header_secret(api_key, label="Search API key")
        if spec.requires_api_key
        else ""
    )
    effective_api_key_env = (
        ""
        if api_key or not spec.requires_api_key
        else api_key_env.strip()
    )
    if (
        not effective_api_key
        and not effective_api_key_env
        and spec.requires_api_key
        and config.search_provider == provider_id
        and config.search_api_key
    ):
        effective_api_key = config.search_api_key
    if spec.requires_api_key and not effective_api_key and not effective_api_key_env:
        raise ValueError(f"search provider {provider_id!r} requires an api_key")

    new_cfg = _clone(config)
    new_cfg.search_provider = provider_id
    new_cfg.search_api_key = effective_api_key
    new_cfg.search_api_key_env = effective_api_key_env
    new_cfg.search_max_results = effective_max_results
    new_cfg.search_proxy = effective_proxy
    new_cfg.search_use_env_proxy = effective_use_env_proxy
    new_cfg.search_fallback_policy = fallback_policy_value
    new_cfg.search_diagnostics = effective_diagnostics
    if api_key:
        clear_runtime_secret_paths(new_cfg, {"search_api_key"})

    api_key_source = (
        "explicit" if effective_api_key else ("env" if effective_api_key_env else "none")
    )
    payload = {
        "provider": provider_id,
        "api_key": effective_api_key,
        "api_key_env": effective_api_key_env,
        "api_key_source": api_key_source,
        "max_results": effective_max_results,
        "proxy": effective_proxy,
        "use_env_proxy": effective_use_env_proxy,
        "fallback_policy": fallback_policy_value,
        "diagnostics": effective_diagnostics,
    }
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=False,
        warnings=[],
        public_payload=redact_search_payload(payload),
    )


def _image_generation_provider_config(config: GatewayConfig, provider_id: str) -> Any:
    providers = config.image_generation.providers
    provider_config = getattr(providers, provider_id, None)
    if provider_config is None:
        raise KeyError(f"unknown image generation provider: {provider_id!r}")
    return provider_config


ImageOutputFormat = Literal["png", "jpeg", "webp"]
ImageGenerationCredentialMode = Literal["direct", "env"]
_VALID_IMAGE_SIZES = ("1024x1024", "1536x1024", "1024x1536")
_VALID_IMAGE_OUTPUT_FORMATS: tuple[ImageOutputFormat, ...] = ("png", "jpeg", "webp")


def _normalize_image_generation_credential_mode(
    value: str | None,
) -> ImageGenerationCredentialMode | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized not in {"direct", "env"}:
        raise ValueError("credential_mode must be 'direct' or 'env'")
    return cast(ImageGenerationCredentialMode, normalized)


def _is_legacy_image_generation_provider_switch_payload(
    *,
    config: GatewayConfig,
    provider_id: str,
    target_spec: Any,
    primary: str,
    api_key_env: str,
    base_url: str | None,
    enabled: bool,
    fallbacks: list[str] | None,
    clear_fallbacks: bool,
    credential_mode: str | None,
) -> bool:
    """Recognize a stale 0.5.0 provider-switch payload.

    The old WebUI changed the selected provider and its default env-var name,
    but retained the source provider's primary, endpoint, and fallback draft.
    Anchor every stale field to the stored source configuration so an arbitrary
    cross-provider RPC request is still rejected.
    """

    if (
        enabled is not True
        or credential_mode is not None
        or clear_fallbacks is not False
        or not isinstance(primary, str)
        or not isinstance(base_url, str)
        or fallbacks is None
        or api_key_env != target_spec.env_key
    ):
        return False
    stored_primary = str(getattr(config.image_generation, "primary", "") or "")
    if primary != stored_primary:
        return False
    try:
        source_provider_id, _source_model = parse_image_generation_model_ref(stored_primary)
        source_spec = get_image_generation_provider_setup_spec(source_provider_id)
        source_provider_cfg = _image_generation_provider_config(config, source_provider_id)
    except (KeyError, ValueError):
        return False
    if source_provider_id == provider_id or not source_spec.runtime_supported:
        return False
    stored_base_url = str(getattr(source_provider_cfg, "base_url", "") or "")
    return (
        base_url == stored_base_url
        and fallbacks == list(getattr(config.image_generation, "fallbacks", []) or [])
    )


def upsert_image_generation_provider(
    config: GatewayConfig,
    *,
    provider_id: str,
    primary: str = "",
    api_key: str = "",
    api_key_env: str = "",
    base_url: str | None = None,
    enabled: bool = True,
    size: str = "",
    output_format: str = "",
    fallbacks: list[str] | None = None,
    clear_fallbacks: bool = False,
    credential_mode: str | None = None,
) -> MutationResult:
    spec = get_image_generation_provider_setup_spec(provider_id)
    if not spec.runtime_supported:
        raise ValueError(
            f"image generation provider {provider_id!r} is not runtime-supported "
            "and cannot be configured"
        )
    if _is_legacy_image_generation_provider_switch_payload(
        config=config,
        provider_id=provider_id,
        target_spec=spec,
        primary=primary,
        api_key_env=api_key_env,
        base_url=base_url,
        enabled=enabled,
        fallbacks=fallbacks,
        clear_fallbacks=clear_fallbacks,
        credential_mode=credential_mode,
    ):
        primary = spec.default_model
        base_url = spec.default_base_url
    stored_primary = str(getattr(config.image_generation, "primary", "") or "")
    requested_primary = primary or (
        stored_primary if not enabled and stored_primary else spec.default_model
    )
    preserve_legacy_primary = not enabled and requested_primary == stored_primary
    try:
        primary_provider, primary_model_name = parse_image_generation_model_ref(
            requested_primary
        )
    except ValueError:
        if not preserve_legacy_primary:
            raise
        primary_provider = provider_id
        primary_model = requested_primary
    else:
        primary_model = f"{primary_provider}/{primary_model_name}"
    if primary_provider != provider_id and not preserve_legacy_primary:
        raise ValueError(
            "primary must be a provider/model reference for "
            f"image generation provider {provider_id!r}"
    )

    # size/output_format are constrained; empty keeps the current value.
    effective_size = (size or "").strip() or config.image_generation.size
    if effective_size not in _VALID_IMAGE_SIZES:
        raise ValueError(
            f"image size must be one of {', '.join(_VALID_IMAGE_SIZES)}"
        )
    effective_output_format = (output_format or "").strip() or config.image_generation.output_format
    if effective_output_format not in _VALID_IMAGE_OUTPUT_FORMATS:
        raise ValueError(
            f"image output format must be one of {', '.join(_VALID_IMAGE_OUTPUT_FORMATS)}"
        )
    if not isinstance(clear_fallbacks, bool):
        raise ValueError("clear_fallbacks must be a boolean")
    if clear_fallbacks and fallbacks is None:
        raise ValueError("clear_fallbacks requires a fallbacks list")
    credential_source = _normalize_image_generation_credential_mode(credential_mode)

    # Older 0.5.0 clients always sent ``fallbacks: []`` for an untouched
    # field, so an empty list remains keep-current unless the new additive
    # clear intent is present. Non-empty lists still replace the chain.
    current_fallbacks = list(config.image_generation.fallbacks)
    cleaned_fallbacks: list[str] = []
    preserve_legacy_fallbacks = (
        not enabled
        and fallbacks is not None
        and list(fallbacks) == current_fallbacks
    )
    if preserve_legacy_fallbacks:
        cleaned_fallbacks = current_fallbacks
    elif fallbacks is not None:
        for fallback in fallbacks:
            if isinstance(fallback, str) and not fallback.strip():
                continue
            try:
                fallback_provider, fallback_model = parse_image_generation_model_ref(fallback)
            except ValueError as exc:
                raise ValueError(
                    f"image fallback {fallback!r} must be a provider/model reference"
                ) from exc
            cleaned_fallbacks.append(f"{fallback_provider}/{fallback_model}")
    fallbacks_updated = (
        not preserve_legacy_fallbacks
        and (bool(cleaned_fallbacks) or clear_fallbacks)
    )
    effective_fallbacks = (
        cleaned_fallbacks
        if fallbacks_updated
        else current_fallbacks
    )

    current_provider_cfg = _image_generation_provider_config(config, provider_id)
    api_key_is_redacted = is_redacted_secret_sentinel(api_key)
    if api_key_is_redacted:
        # Round-tripped redaction mask: keep the stored key (see
        # upsert_llm_provider for the server-side trust-boundary rationale).
        api_key = ""
    explicit_env_key = _clean_optional_str(api_key_env)
    if credential_source == "direct":
        # New clients state which credential control was edited. The direct
        # value wins even if a stale draft still carries an env name.
        explicit_env_key = ""
    elif credential_source == "env":
        # Conversely, switching to an env reference intentionally replaces a
        # stored direct key even when the write-only key field is redacted.
        api_key = ""
    elif api_key_is_redacted:
        # A legacy read-modify-write echo is never an instruction to switch
        # sources. Preserve the stored direct key and ignore display state.
        explicit_env_key = ""
    elif api_key and explicit_env_key:
        # The 0.5.0 WebUI keeps the selected provider's default env name in
        # the form while a user pastes a direct key. Treat only that default
        # echo as non-authoritative; a custom env plus a direct key remains
        # ambiguous and is rejected.
        if explicit_env_key == spec.env_key:
            explicit_env_key = ""
        else:
            raise ValueError("configure either api_key or api_key_env, not both")
    elif (
        not api_key
        and explicit_env_key == spec.env_key
        and bool(getattr(current_provider_cfg, "api_key", ""))
    ):
        # Old clients send their default env field during every save. Without
        # a credentialMode it must not erase a stored direct key.
        explicit_env_key = ""
    stored_base_url = str(getattr(current_provider_cfg, "base_url", "") or "")
    if base_url is None:
        effective_base_url = resolve_image_generation_base_url(
            provider_id=provider_id,
            provider_config=current_provider_cfg,
            llm_config=config.llm,
            default_base_url=spec.default_base_url,
            gateway_config=config,
        )
    else:
        effective_base_url = str(base_url).strip() or spec.default_base_url
    preserve_legacy_base_url = (
        not enabled
        and bool(stored_base_url)
        and effective_base_url == stored_base_url
    )
    if (
        not is_valid_image_generation_base_url(effective_base_url)
        and not preserve_legacy_base_url
    ):
        raise ValueError("image base_url must be an absolute http:// or https:// URL")
    conflicting_provider = conflicting_image_generation_endpoint_provider(
        provider_id,
        effective_base_url,
    )
    if enabled and conflicting_provider:
        raise ValueError(
            f"image generation provider {provider_id!r} cannot use the official "
            f"{conflicting_provider!r} endpoint; use {spec.default_base_url!r} "
            "or a custom compatible endpoint"
        )
    # A stored credential must not follow a changed endpoint origin: on a
    # scheme/host/effective-port change every reusable secret source —
    # including the well-known registry default env var — is dropped
    # fail-closed so the operator re-enters it for the new endpoint. This
    # mirrors the profile-save boundary.
    endpoint_allows_reuse = base_url_allows_credential_reuse(
        stored_base_url or spec.default_base_url,
        effective_base_url,
    )
    stored_api_key = (
        str(getattr(current_provider_cfg, "api_key", "") or "")
        if endpoint_allows_reuse
        else ""
    )
    # A newly selected env reference replaces a stored direct key. Without
    # this branch the runtime keeps preferring the old direct key, so the UI
    # appears to switch sources while the effective credential does not.
    replace_stored_api_key = credential_source == "env" or bool(explicit_env_key)
    effective_api_key = clean_header_secret(
        api_key or ("" if replace_stored_api_key else stored_api_key),
        label="Image API key",
    )
    stored_env_key = str(getattr(current_provider_cfg, "api_key_env", spec.env_key) or "")
    stored_env_is_explicit = "api_key_env" in set(
        getattr(current_provider_cfg, "model_fields_set", set())
    )
    if (
        not endpoint_allows_reuse
        and credential_source != "env"
        and explicit_env_key == stored_env_key
    ):
        # Clients hydrate and re-send the stored env-var name verbatim: a
        # re-submitted value equal to the stored reference means "keep the
        # current credential", not a credential authored for the changed
        # endpoint origin, so it is gated like every stored source.
        explicit_env_key = ""
    current_env_key = (
        stored_env_key if endpoint_allows_reuse and stored_env_is_explicit else ""
    )
    if credential_source == "direct" or api_key:
        env_key = ""
    elif credential_source == "env":
        env_key = explicit_env_key
    elif explicit_env_key:
        env_key = explicit_env_key
    else:
        # Do not materialize the schema-default env name into a live config.
        # The shared resolver may still use it at the registry endpoint, but
        # it remains an implicit fallback rather than an authored reference.
        env_key = current_env_key
    has_saved_env_reference = bool(explicit_env_key or current_env_key)

    new_cfg = _clone(config)
    new_cfg.image_generation.enabled = bool(enabled)
    # Any direct image-provider save transfers ownership away from the
    # provider-following default, including an explicit disable payload.
    new_cfg.image_generation.binding = "custom"
    # The enabled decision is explicit at this layer (callers resolve
    # keep-current before invoking): force it into the file even when it
    # equals the model default, otherwise a first-time enabled=false is
    # dropped by the sparse persist and a later key rotation flips the tool
    # back on via the legacy configure-implies-enable fallback.
    new_cfg.mark_force_persist("image_generation.enabled")
    new_cfg.mark_force_persist("image_generation.binding")
    new_cfg.image_generation.primary = primary_model
    new_cfg.image_generation.size = effective_size
    new_cfg.image_generation.output_format = cast(ImageOutputFormat, effective_output_format)
    new_cfg.image_generation.fallbacks = effective_fallbacks
    if fallbacks_updated:
        new_cfg.mark_force_persist("image_generation.fallbacks")
    next_provider_cfg = _image_generation_provider_config(new_cfg, provider_id)
    next_provider_cfg.api_key = effective_api_key
    next_provider_cfg.api_key_env = env_key
    next_provider_cfg.base_url = effective_base_url
    next_fields_set = getattr(next_provider_cfg, "model_fields_set", None)
    if isinstance(next_fields_set, set):
        if explicit_env_key or current_env_key:
            next_fields_set.add("api_key_env")
        else:
            next_fields_set.discard("api_key_env")
    credential_resolution = resolve_image_generation_credential(
        provider_id=provider_id,
        provider_config=next_provider_cfg,
        default_env_key=spec.env_key,
        default_base_url=spec.default_base_url,
        effective_base_url=effective_base_url,
        gateway_config=new_cfg,
        model=primary_model,
    )
    api_key_source = credential_resolution.source
    if (
        enabled
        and spec.requires_api_key
        and not credential_resolution.available
        and not has_saved_env_reference
    ):
        # Fallback routes are executable candidates, so a missing credential
        # on this primary does not make an otherwise healthy chain unusable.
        # Consult the shared onboarding verifier only after the prospective
        # selected-provider state has been applied.
        from opensquilla.onboarding.section_status import (
            SectionStatus,
            image_generation_section_status,
        )

        if image_generation_section_status(new_cfg) is not SectionStatus.OK:
            raise ValueError(
                f"image generation provider {provider_id!r} requires an api_key, "
                f"{spec.env_key}, or a matching configured model-service provider"
            )
    if base_url is not None:
        new_cfg.mark_force_persist(
            f"image_generation.providers.{provider_id}.base_url"
        )
    if explicit_env_key == spec.env_key and not base_url_allows_credential_reuse(
        spec.default_base_url,
        effective_base_url,
    ):
        # Preserve authorship when the operator deliberately binds the
        # registry-named env var to a custom endpoint. Without force-persist,
        # a value equal to the model default may disappear from sparse TOML.
        new_cfg.mark_force_persist(
            f"image_generation.providers.{provider_id}.api_key_env"
        )
    if api_key:
        clear_runtime_secret_paths(
            new_cfg, {f"image_generation.providers.{provider_id}.api_key"}
        )

    payload = {
        "provider": provider_id,
        "enabled": bool(enabled),
        "primary": primary_model,
        "api_key": effective_api_key,
        "api_key_env": env_key,
        "api_key_source": api_key_source,
        "base_url": effective_base_url,
        "size": effective_size,
        "output_format": effective_output_format,
        "fallbacks": effective_fallbacks,
    }
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=False,
        warnings=[],
        public_payload=redact_image_generation_payload(payload),
    )


def disable_image_generation(config: GatewayConfig) -> MutationResult:
    new_cfg = _clone(config)
    new_cfg.image_generation.enabled = False
    new_cfg.image_generation.binding = "custom"
    # Explicit off switch: must land in the file even on a fresh config where
    # it equals the model default, so a later provider save that omits the
    # flag keeps it off instead of re-enabling via configure-implies-enable.
    new_cfg.mark_force_persist("image_generation.enabled")
    new_cfg.mark_force_persist("image_generation.binding")
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=False,
        warnings=[],
        public_payload={
            "enabled": False,
            "primary": new_cfg.image_generation.primary,
        },
    )


def _audio_provider_config(config: GatewayConfig, provider_id: str) -> Any:
    providers = config.audio.providers
    provider_config = getattr(providers, provider_id, None)
    if provider_config is None:
        raise KeyError(f"unknown audio provider: {provider_id!r}")
    return provider_config


def _audio_api_key_source(*, api_key: str, env_key: str) -> str:
    if api_key:
        return "explicit"
    if env_key and os.environ.get(env_key):
        return "env"
    if env_key:
        return "missing_env"
    return "none"


def upsert_audio_provider(
    config: GatewayConfig,
    *,
    provider_id: str,
    api_key: str = "",
    api_key_env: str = "",
    base_url: str = "",
    enabled: bool = True,
    tts_voice: str = "",
    tts_model: str = "",
    language_code: str = "",
) -> MutationResult:
    spec = get_audio_provider_setup_spec(provider_id)
    if not spec.runtime_supported:
        raise ValueError(
            f"audio provider {provider_id!r} is not runtime-supported and cannot be configured"
        )
    if provider_id != "elevenlabs":
        raise ValueError(f"audio provider {provider_id!r} is not supported")

    current_provider_cfg = _audio_provider_config(config, provider_id)
    if is_redacted_secret_sentinel(api_key):
        # Round-tripped redaction mask: keep the stored key (see
        # upsert_llm_provider for the server-side trust-boundary rationale).
        api_key = ""
    explicit_env_key = _clean_optional_str(api_key_env)
    if api_key and explicit_env_key:
        raise ValueError("configure either api_key or api_key_env, not both")
    stored_base_url = str(getattr(current_provider_cfg, "base_url", "") or "")
    effective_base_url = base_url or stored_base_url or spec.default_base_url
    # A stored credential must not follow a changed endpoint origin: on a
    # scheme/host/effective-port change every reusable secret source —
    # including the well-known registry default env var — is dropped
    # fail-closed so the operator re-enters it for the new endpoint. This
    # mirrors the profile-save boundary.
    endpoint_allows_reuse = base_url_allows_credential_reuse(
        stored_base_url or spec.default_base_url,
        effective_base_url,
    )
    stored_api_key = (
        str(getattr(current_provider_cfg, "api_key", "") or "")
        if endpoint_allows_reuse
        else ""
    )
    effective_api_key = clean_header_secret(
        api_key or stored_api_key,
        label="Audio API key",
    )
    stored_env_key = str(getattr(current_provider_cfg, "api_key_env", spec.env_key) or "")
    if not endpoint_allows_reuse and explicit_env_key == stored_env_key:
        # Clients hydrate and re-send the stored env-var name verbatim: a
        # re-submitted value equal to the stored reference means "keep the
        # current credential", not a credential authored for the changed
        # endpoint origin, so it is gated like every stored source.
        explicit_env_key = ""
    current_env_key = stored_env_key if endpoint_allows_reuse else ""
    # Bind the implicit registry env name to the registry endpoint rather
    # than the previously stored endpoint, closing disable-then-enable
    # recovery at a foreign origin.
    default_env_key = (
        spec.env_key
        if base_url_allows_credential_reuse(spec.default_base_url, effective_base_url)
        else ""
    )
    env_key = "" if api_key else (explicit_env_key or current_env_key or default_env_key)
    api_key_source = _audio_api_key_source(
        api_key=effective_api_key,
        env_key=env_key,
    )
    if enabled and spec.requires_api_key and api_key_source == "none":
        raise ValueError(
            f"audio provider {provider_id!r} requires an api_key or {spec.env_key}"
        )

    effective_tts_voice = tts_voice or config.audio.tts.voice or spec.default_tts_voice
    effective_tts_model = tts_model or config.audio.tts.model or spec.default_tts_model
    effective_language_code = language_code or config.audio.tts.language_code

    new_cfg = _clone(config)
    new_cfg.audio.enabled = bool(enabled)
    # Preserve an explicit legacy-client disabled decision even though false
    # equals the schema default and sparse persistence would otherwise omit it.
    new_cfg.mark_force_persist("audio.enabled")
    next_provider_cfg = _audio_provider_config(new_cfg, provider_id)
    next_provider_cfg.api_key = effective_api_key
    next_provider_cfg.api_key_env = env_key
    next_provider_cfg.base_url = effective_base_url
    if explicit_env_key == spec.env_key and not base_url_allows_credential_reuse(
        spec.default_base_url,
        effective_base_url,
    ):
        new_cfg.mark_force_persist(
            f"audio.providers.{provider_id}.api_key_env"
        )
    new_cfg.audio.tts.voice = effective_tts_voice
    new_cfg.audio.tts.model = effective_tts_model
    new_cfg.audio.tts.language_code = effective_language_code
    if api_key:
        clear_runtime_secret_paths(new_cfg, {f"audio.providers.{provider_id}.api_key"})

    payload = {
        "provider": provider_id,
        "enabled": bool(enabled),
        "api_key": effective_api_key,
        "api_key_env": env_key,
        "api_key_source": api_key_source,
        "base_url": effective_base_url,
        "tts_voice": effective_tts_voice,
        "tts_model": effective_tts_model,
        "language_code": effective_language_code,
    }
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=False,
        warnings=[],
        public_payload=redact_audio_payload(payload),
    )


def upsert_memory_embedding(
    config: GatewayConfig,
    *,
    provider: str,
    model: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    base_url: str | None = None,
    onnx_dir: str | None = None,
) -> MutationResult:
    if provider not in {"auto", "none", "local", "openai", "openai-compatible", "ollama"}:
        raise ValueError(f"unknown memory embedding provider: {provider!r}")

    new_cfg = _clone(config)
    old_memory = config.memory.model_dump(mode="python")
    current = config.memory.embedding
    model_value = _clean_optional_str(model)
    if is_redacted_secret_sentinel(api_key):
        # Round-tripped redaction mask: keep the stored key (see
        # upsert_llm_provider for the server-side trust-boundary rationale).
        api_key = None
    api_key_value = _clean_optional_str(api_key)
    api_key_env_value = _clean_optional_str(api_key_env)
    if api_key_value and api_key_env_value:
        raise ValueError("configure either api_key or api_key_env, not both")
    base_url_value = _clean_optional_str(base_url)
    onnx_dir_value = _clean_optional_str(onnx_dir)
    payload: dict[str, Any] = {"provider": provider}

    if provider in _REMOTE_MEMORY_EMBEDDING_PROVIDERS:
        current_api_key_env = _clean_optional_str(
            getattr(current.remote, "api_key_env", None)
        )
        stored_base_url = current.remote.base_url or current.base_url or ""
        effective_base_url = (
            base_url_value or stored_base_url or _DEFAULT_REMOTE_EMBEDDING_BASE_URL
        )
        # A stored credential must not follow a changed endpoint origin: on a
        # scheme/host/effective-port change every reusable secret source is
        # dropped fail-closed (the required-key check below then forces the
        # operator to re-enter it), mirroring the profile-save boundary.
        endpoint_allows_reuse = base_url_allows_credential_reuse(
            stored_base_url or _DEFAULT_REMOTE_EMBEDDING_BASE_URL,
            effective_base_url,
        )
        reusable_stored_env = current_api_key_env if endpoint_allows_reuse else None
        reusable_stored_key = (
            (current.remote.api_key or current.api_key or "")
            if endpoint_allows_reuse
            else ""
        )
        submitted_env_key = api_key_env_value
        if not endpoint_allows_reuse and submitted_env_key == current_api_key_env:
            # Clients hydrate and re-send the stored env-var name verbatim:
            # a re-submitted value equal to the stored reference means "keep
            # the current credential" and must not follow the changed origin.
            submitted_env_key = ""
        effective_api_key_env = "" if api_key_value else (
            submitted_env_key or reusable_stored_env or ""
        )
        effective_api_key = (
            api_key_value
            or ("" if effective_api_key_env else reusable_stored_key)
        )
        if not effective_api_key and not effective_api_key_env:
            raise ValueError(
                "remote memory embedding provider requires an api_key or api_key_env"
            )
        payload["remote"] = {"base_url": effective_base_url}
        if effective_api_key:
            payload["remote"]["api_key"] = effective_api_key
        if effective_api_key_env:
            payload["remote"]["api_key_env"] = effective_api_key_env
        remote_model = model_value or current.remote.model or current.model
        if remote_model:
            payload["remote"]["model"] = remote_model
    elif provider == "auto":
        remote_payload: dict[str, str] = {}
        current_api_key_env = _clean_optional_str(
            getattr(current.remote, "api_key_env", None)
        )
        stored_base_url = current.remote.base_url or current.base_url or ""
        remote_base_url = base_url_value or stored_base_url
        # A stored remote credential must not follow a changed configured
        # endpoint origin; an omitted/blank base keeps the stored origin.
        endpoint_allows_reuse = base_url_allows_credential_reuse(
            stored_base_url,
            remote_base_url,
        )
        reusable_stored_env = current_api_key_env if endpoint_allows_reuse else None
        reusable_stored_key = (
            (current.remote.api_key or current.api_key or "")
            if endpoint_allows_reuse
            else ""
        )
        submitted_env_key = api_key_env_value
        if not endpoint_allows_reuse and submitted_env_key == current_api_key_env:
            # Clients hydrate and re-send the stored env-var name verbatim:
            # a re-submitted value equal to the stored reference means "keep
            # the current credential" and must not follow the changed origin.
            submitted_env_key = ""
        effective_api_key_env = "" if api_key_value else (
            submitted_env_key or reusable_stored_env or ""
        )
        effective_api_key = (
            api_key_value
            or ("" if effective_api_key_env else reusable_stored_key)
        )
        if effective_api_key:
            remote_payload["api_key"] = effective_api_key
        if effective_api_key_env:
            remote_payload["api_key_env"] = effective_api_key_env
        if remote_base_url:
            remote_payload["base_url"] = remote_base_url
        remote_model = model_value or current.remote.model or (
            current.model if (effective_api_key or effective_api_key_env) else None
        )
        if remote_model:
            remote_payload["model"] = remote_model
        if remote_payload:
            payload["remote"] = remote_payload
    elif provider == "local":
        payload["local"] = {}
        local_onnx_dir = onnx_dir_value or (
            current.local.onnx_dir if current.requested_provider == "local" else ""
        )
        if local_onnx_dir:
            payload["local"]["onnx_dir"] = local_onnx_dir
    elif provider == "ollama":
        payload["ollama"] = {
            "base_url": (
                base_url_value
                or current.ollama.base_url
                or _DEFAULT_OLLAMA_EMBEDDING_BASE_URL
            ),
        }
        ollama_model = model_value or current.ollama.model
        if ollama_model:
            payload["ollama"]["model"] = ollama_model

    new_cfg.memory.embedding = MemoryEmbeddingConfig.model_validate(payload)
    changed = old_memory != new_cfg.memory.model_dump(mode="python")
    if api_key_value or api_key_env_value:
        clear_runtime_secret_paths(
            new_cfg,
            {"memory.embedding.remote.api_key", "memory.embedding.api_key"},
        )

    return MutationResult(
        config=new_cfg,
        changed=changed,
        restart_required=changed,
        warnings=[],
        public_payload=redact_memory_embedding_payload(payload),
    )


def _forget_reset_provenance(
    config: GatewayConfig,
    *,
    remove_paths: tuple[str, ...],
    secret_paths: set[str],
) -> None:
    """Drop runtime provenance that no longer describes the reset section."""

    forget_secret_provenance_paths(config, secret_paths)
    prefixes = tuple(f"{path}." for path in remove_paths)
    for path in tuple(config.runtime_field_overrides()):
        if path in remove_paths or path.startswith(prefixes):
            config.clear_runtime_override(path)


def _persisted_capability_payload(config: GatewayConfig) -> Mapping[str, Any] | None:
    """Return managed TOML state, distinguishing no-file from no snapshot.

    Loaded configs carry ``_persist_baseline`` even when no TOML file exists.
    In that case the empty mapping is authoritative and environment-only
    effective values must not make the client advertise a removable config.
    Directly constructed test/integration configs have no snapshot and retain
    the historical effective-model fallback below.
    """

    raw = getattr(config, "_persist_raw_base", None)
    if isinstance(raw, Mapping):
        return raw
    if getattr(config, "_persist_baseline", None) is not None:
        return {}
    return None


def capability_resettable(config: GatewayConfig, *, capability_id: str) -> bool:
    """Return whether a capability differs from its canonical client default."""

    capability = str(capability_id or "").strip().lower()
    persisted = _persisted_capability_payload(config)
    if persisted is not None:
        if capability == "search":
            search_keys = (
                "search_provider",
                "search_api_key",
                "search_api_key_env",
                "search_max_results",
                "search_proxy",
                "search_use_env_proxy",
                "search_fallback_policy",
                "search_diagnostics",
            )
            search_managed = {
                key: persisted[key] for key in search_keys if key in persisted
            }
            return search_managed not in ({}, {"search_provider": "duckduckgo"})
        if capability == "image_generation":
            image_managed = persisted.get("image_generation")
            if image_managed is None:
                return False
            return not (
                isinstance(image_managed, Mapping)
                and dict(image_managed)
                in (
                    {},
                    {"enabled": False},
                    {"enabled": False, "binding": "custom"},
                )
            )
        if capability == "audio":
            audio_managed = persisted.get("audio")
            if audio_managed is None:
                return False
            return not (
                isinstance(audio_managed, Mapping)
                and dict(audio_managed) in ({}, {"enabled": False})
            )
        if capability == "memory_embedding":
            memory = persisted.get("memory")
            if not isinstance(memory, Mapping) or "embedding" not in memory:
                return False
            memory_managed = memory["embedding"]
            return not (
                isinstance(memory_managed, Mapping)
                and dict(memory_managed) in ({}, {"provider": "auto"})
            )
        raise ValueError(f"unknown capability: {capability_id!r}")

    if capability == "search":
        return (
            config.search_provider,
            config.search_api_key,
            config.search_api_key_env,
            config.search_max_results,
            config.search_proxy,
            config.search_use_env_proxy,
            config.search_fallback_policy,
            config.search_diagnostics,
        ) != (
            "duckduckgo",
            "",
            "",
            DEFAULT_SEARCH_MAX_RESULTS,
            "",
            False,
            "off",
            False,
        )
    if capability == "image_generation":
        default_image = ImageGenerationConfig.model_construct()
        return config.image_generation.model_dump(mode="python") != (
            default_image.model_dump(mode="python")
        )
    if capability == "audio":
        default_audio = AudioConfig.model_construct()
        return config.audio.model_dump(mode="python") != (
            default_audio.model_dump(mode="python")
        )
    if capability == "memory_embedding":
        return config.memory.embedding.model_dump(mode="python") != (
            MemoryEmbeddingConfig().model_dump(mode="python")
        )
    raise ValueError(f"unknown capability: {capability_id!r}")


def reset_capability(config: GatewayConfig, *, capability_id: str) -> MutationResult:
    """Restore one client-facing capability to its built-in configuration."""

    capability = str(capability_id or "").strip().lower()
    new_cfg = _clone(config)
    restart_required = False
    remove_paths: tuple[str, ...]

    if capability == "search":
        changed = capability_resettable(config, capability_id=capability)
        new_cfg.search_provider = "duckduckgo"
        new_cfg.search_api_key = ""
        new_cfg.search_api_key_env = ""
        new_cfg.search_max_results = DEFAULT_SEARCH_MAX_RESULTS
        new_cfg.search_proxy = ""
        new_cfg.search_use_env_proxy = False
        new_cfg.search_fallback_policy = "off"
        new_cfg.search_diagnostics = False
        remove_paths = (
            "search_provider",
            "search_api_key",
            "search_api_key_env",
            "search_max_results",
            "search_proxy",
            "search_use_env_proxy",
            "search_fallback_policy",
            "search_diagnostics",
        )
        new_cfg.mark_force_persist("search_provider")
        _forget_reset_provenance(
            new_cfg,
            remove_paths=remove_paths,
            secret_paths={"search_api_key"},
        )
    elif capability == "image_generation":
        # model_construct applies schema defaults without consulting
        # BaseSettings environment sources. The environment itself remains
        # untouched; only OpenSquilla-managed config is removed.
        default_image = ImageGenerationConfig.model_construct()
        changed = capability_resettable(config, capability_id=capability)
        new_cfg.image_generation = default_image
        remove_paths = ("image_generation",)
        new_cfg.mark_force_persist("image_generation.enabled")
        image_secret_paths = {
            f"image_generation.providers.{provider_id}.api_key"
            for provider_id in type(default_image.providers).model_fields
        }
        _forget_reset_provenance(
            new_cfg,
            remove_paths=remove_paths,
            secret_paths=image_secret_paths,
        )
    elif capability == "audio":
        default_audio = AudioConfig.model_construct()
        changed = capability_resettable(config, capability_id=capability)
        new_cfg.audio = default_audio
        remove_paths = ("audio",)
        new_cfg.mark_force_persist("audio.enabled")
        audio_secret_paths = {
            f"audio.providers.{provider_id}.api_key"
            for provider_id in type(default_audio.providers).model_fields
        }
        _forget_reset_provenance(
            new_cfg,
            remove_paths=remove_paths,
            secret_paths=audio_secret_paths,
        )
    elif capability == "memory_embedding":
        default_embedding = MemoryEmbeddingConfig()
        changed = capability_resettable(config, capability_id=capability)
        new_cfg.memory.embedding = default_embedding
        remove_paths = ("memory.embedding",)
        new_cfg.mark_force_persist("memory.embedding.provider")
        _forget_reset_provenance(
            new_cfg,
            remove_paths=remove_paths,
            secret_paths={
                "memory.embedding.api_key",
                "memory.embedding.remote.api_key",
            },
        )
        restart_required = True
    else:
        raise ValueError(f"unknown capability: {capability_id!r}")

    return MutationResult(
        config=new_cfg,
        changed=changed,
        restart_required=restart_required,
        warnings=[],
        public_payload={"capabilityId": capability, "reset": True},
        remove_paths=remove_paths,
    )


def _llm_profile_storage_keys(
    config: GatewayConfig,
    provider_id: str,
) -> tuple[str, ...]:
    """Find exact and historical case-variant keys for one provider profile."""
    provider = str(provider_id or "").strip().lower()
    profiles = getattr(config, "llm_profiles", None) or {}
    exact = [key for key in profiles if str(key) == provider]
    variants = [
        key
        for key in profiles
        if str(key) != provider and str(key).strip().lower() == provider
    ]
    return tuple(exact + variants)


def _profile_reference_labels(config: GatewayConfig, provider_id: str) -> list[str]:
    """Return stable, non-secret config paths that reference a provider profile."""
    provider = str(provider_id or "").strip().lower()
    references: list[str] = []
    tiers = getattr(getattr(config, "squilla_router", None), "tiers", {}) or {}
    if isinstance(tiers, Mapping):
        for tier_name, tier in tiers.items():
            if not isinstance(tier, Mapping):
                continue
            tier_provider = str(tier.get("provider") or "").strip().lower()
            if tier_provider == provider:
                references.append(f"squilla_router.tiers.{tier_name}")

    ensemble = getattr(config, "llm_ensemble", None)
    if ensemble is not None:
        for index, candidate in enumerate(getattr(ensemble, "candidates", None) or []):
            candidate_provider = str(getattr(candidate, "provider", "") or "").strip().lower()
            if candidate_provider == provider:
                references.append(f"llm_ensemble.candidates.{index}")

        selection_mode = str(getattr(ensemble, "selection_mode", "") or "")
        static_provider = STATIC_B5_SELECTION_MODE_PROVIDERS.get(selection_mode, "")
        if static_provider == provider:
            references.append("llm_ensemble.selection_mode")
    return references


def upsert_llm_profile(
    config: GatewayConfig,
    *,
    provider_id: str,
    model: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    api_key_env_pool: list[str] | tuple[str, ...] | None = None,
    preserve_api_key: bool = False,
    base_url: str | None = None,
    proxy: str | None = None,
) -> MutationResult:
    """Create or update one non-primary provider deployment profile.

    This additive mutation deliberately permits credential-less drafts.  An
    omitted field keeps its current value, while an explicit empty value
    clears it.  ``preserve_api_key`` is the password-field keep-current
    affordance.  Stored credentials never follow a changed endpoint origin;
    on such a change every omitted credential source is cleared fail-closed.
    """
    provider = str(provider_id or "").strip().lower()
    spec = get_provider_setup_spec(provider)
    if not spec.runtime_supported:
        raise ValueError(
            f"provider {provider!r} is not runtime-supported and cannot be configured"
        )
    if is_redacted_secret_sentinel(api_key):
        # A round-tripped redaction mask means "keep the stored key" — the
        # same server-side trust boundary as upsert_llm_provider. This also
        # covers the draft-probe path, which builds its in-memory draft here:
        # a probe of a masked payload must run with the stored credential,
        # not with a literal '***' bearer token.
        api_key = None
        preserve_api_key = True

    profile_keys = _llm_profile_storage_keys(config, provider)
    existing_key = profile_keys[0] if profile_keys else None
    existing = (
        (getattr(config, "llm_profiles", None) or {}).get(existing_key)
        if existing_key is not None
        else None
    )
    effective_model = (
        str(getattr(existing, "model", "") or "").strip()
        if model is None
        else str(model or "").strip()
    )
    existing_base_url = str(getattr(existing, "base_url", "") or "").strip()
    if base_url is None:
        effective_base_url = existing_base_url
    else:
        effective_base_url = str(base_url or "").strip()
    if spec.requires_base_url and not (effective_base_url or spec.default_base_url):
        raise ValueError(f"provider {provider!r} requires a base_url")

    old_endpoint = existing_base_url or str(spec.default_base_url or "").strip()
    next_endpoint = effective_base_url or str(spec.default_base_url or "").strip()
    endpoint_allows_reuse = existing is not None and base_url_allows_credential_reuse(
        old_endpoint,
        next_endpoint,
    )

    if api_key is None:
        effective_api_key = (
            str(getattr(existing, "api_key", "") or "")
            if endpoint_allows_reuse and preserve_api_key
            else ""
        )
    else:
        effective_api_key = clean_header_secret(api_key, label="LLM profile API key")

    if api_key_env is None:
        effective_api_key_env = (
            str(getattr(existing, "api_key_env", "") or "").strip()
            if endpoint_allows_reuse
            else ""
        )
    else:
        effective_api_key_env = str(api_key_env or "").strip()

    if api_key_env_pool is None:
        effective_pool = (
            list(getattr(existing, "api_key_env_pool", None) or [])
            if endpoint_allows_reuse
            else []
        )
    else:
        effective_pool = []
        seen_pool_names: set[str] = set()
        for value in api_key_env_pool:
            name = str(value or "").strip()
            if name and name not in seen_pool_names:
                seen_pool_names.add(name)
                effective_pool.append(name)

    if proxy is None:
        effective_proxy = str(getattr(existing, "proxy", "") or "")
    else:
        effective_proxy = str(proxy or "").strip()

    profile = LlmProviderProfile(
        model=effective_model,
        api_key=effective_api_key,
        api_key_env=effective_api_key_env,
        api_key_env_pool=effective_pool,
        base_url=effective_base_url,
        proxy=effective_proxy,
    )
    new_cfg = _clone(config)
    new_cfg.llm_profiles = dict(new_cfg.llm_profiles)
    for key in profile_keys:
        new_cfg.llm_profiles.pop(key, None)
    new_cfg.llm_profiles[provider] = profile
    old_runtime_paths = {
        path
        for path in getattr(new_cfg, "_runtime_secret_paths", set())
        if any(
            path == f"llm_profiles.{key}"
            or path.startswith(f"llm_profiles.{key}.")
            for key in profile_keys
        )
    }
    preserve_runtime_api_key = bool(
        api_key is None
        and effective_api_key
        and existing_key is not None
        and f"llm_profiles.{existing_key}.api_key"
        in getattr(config, "_runtime_secret_paths", set())
    )
    clear_runtime_secret_paths(new_cfg, old_runtime_paths)
    if preserve_runtime_api_key:
        new_cfg.mark_runtime_secret(f"llm_profiles.{provider}.api_key")
    # An explicitly supplied secret is operator-authored and must persist;
    # an explicit clear likewise invalidates any inherited runtime marker.
    if api_key is not None:
        clear_runtime_secret_paths(new_cfg, {f"llm_profiles.{provider}.api_key"})

    public_payload = profile.model_dump(mode="python")
    public_payload["provider"] = provider
    return MutationResult(
        config=new_cfg,
        changed=new_cfg.llm_profiles != config.llm_profiles,
        restart_required=False,
        public_payload=redact_provider_payload(public_payload),
    )


def clear_llm_profile_credentials(
    config: GatewayConfig,
    *,
    provider_id: str,
) -> MutationResult:
    """Remove stored credential sources while keeping a provider profile.

    All case variants of the requested profile key are cleared so a malformed
    historical config cannot retain a duplicate secret.  Model and transport
    settings stay unchanged, as do every Router and Ensemble reference.
    """
    provider = str(provider_id or "").strip().lower()
    active_provider = str(config.llm.provider or "").strip().lower()
    if not provider:
        raise ValueError("providerId is required")
    if provider == active_provider:
        raise ValueError("active provider credentials use the provider clear operation")
    profile_keys = _llm_profile_storage_keys(config, provider)
    if not profile_keys:
        raise KeyError(f"LLM profile {provider!r} does not exist")

    new_cfg = _clone(config)
    new_cfg.llm_profiles = dict(new_cfg.llm_profiles)
    changed = False
    for key in profile_keys:
        profile = new_cfg.llm_profiles[key]
        payload = profile.model_dump(mode="python")
        changed = changed or bool(
            payload.get("api_key")
            or payload.get("api_key_env")
            or payload.get("api_key_env_pool")
        )
        payload.update(api_key="", api_key_env="", api_key_env_pool=[])
        new_cfg.llm_profiles[key] = LlmProviderProfile(**payload)

    provenance_paths = {
        f"llm_profiles.{key}.api_key"
        for key in profile_keys
    }
    for path in provenance_paths:
        if path in getattr(new_cfg, "_runtime_secret_paths", set()) or path in getattr(
            new_cfg, "_explicit_secret_paths", set()
        ):
            changed = True
        new_cfg._runtime_secret_paths.discard(path)
        new_cfg._explicit_secret_paths.discard(path)

    return MutationResult(
        config=new_cfg,
        changed=changed,
        restart_required=False,
        public_payload={
            "provider": provider,
            "active": False,
            "storedCredentialsCleared": True,
        },
    )


def remove_llm_profile(config: GatewayConfig, *, provider_id: str) -> MutationResult:
    """Remove an unused provider profile, refusing dangling route references."""
    provider = str(provider_id or "").strip().lower()
    profile_keys = _llm_profile_storage_keys(config, provider)
    if not profile_keys:
        raise KeyError(f"LLM profile {provider!r} does not exist")
    references = _profile_reference_labels(config, provider)
    if references:
        joined = ", ".join(references)
        raise ValueError(f"LLM profile {provider!r} is still referenced by: {joined}")

    new_cfg = _clone(config)
    new_cfg.llm_profiles = dict(new_cfg.llm_profiles)
    for key in profile_keys:
        new_cfg.llm_profiles.pop(key, None)
    runtime_paths = {
        path
        for path in getattr(new_cfg, "_runtime_secret_paths", set())
        if any(
            path == f"llm_profiles.{key}"
            or path.startswith(f"llm_profiles.{key}.")
            for key in profile_keys
        )
    }
    clear_runtime_secret_paths(new_cfg, runtime_paths)
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=False,
        public_payload={"provider": provider, "removed": True},
    )


def _stored_runtime_field(config: GatewayConfig, path: str, current: str) -> str:
    """Return the persisted value behind an in-place runtime env override."""
    overrides = config.runtime_field_overrides()
    stored_applied = overrides.get(path)
    if stored_applied is None:
        return current
    stored, applied = stored_applied
    return str(stored or "") if current == applied else current


def activate_llm_profile(
    config: GatewayConfig,
    *,
    provider_id: str,
    model: str | None = None,
    router_action: str | None = None,
    image_generation_intent: str | None = None,
) -> MutationResult:
    """Atomically promote a stored profile and demote the current primary.

    The mutation is pure: callers must persist the returned candidate before
    applying it to the running gateway.  Managed Router presets follow the new
    primary; custom/legacy Router state and all Ensemble fields remain intact
    unless ``router_action`` explicitly resolves a provider conflict. Image
    defaults likewise require an explicit ``image_generation_intent``.
    """
    from opensquilla.provider.deployment import resolve_provider_deployment

    provider = str(provider_id or "").strip().lower()
    previous_provider = str(config.llm.provider or "").strip().lower()
    if provider == previous_provider:
        raise LlmProfileActivationError(
            "already_active", f"provider {provider!r} is already active"
        )

    profile_keys = _llm_profile_storage_keys(config, provider)
    if not profile_keys:
        raise LlmProfileActivationError(
            "profile_not_found", f"LLM profile {provider!r} does not exist"
        )
    profile_key = profile_keys[0]
    profile = config.llm_profiles[profile_key]
    if list(getattr(profile, "api_key_env_pool", None) or []):
        raise LlmProfileActivationError(
            "primary_pool_unsupported",
            "primary_pool_unsupported: the primary provider does not support api_key_env_pool",
        )

    spec = get_provider_setup_spec(provider)
    model_id = (
        str(model or "").strip()
        or str(getattr(profile, "model", "") or "").strip()
        or str(spec.default_direct_model or "").strip()
    )
    if not model_id:
        raise LlmProfileActivationError(
            "missing_model",
            f"LLM profile {provider!r} has no direct/fallback model",
        )

    resolution = resolve_provider_deployment(config, provider, model_id)
    if not resolution.ready:
        reason = resolution.reason or "not_executable"
        raise LlmProfileActivationError(
            reason,
            f"LLM profile {provider!r} is not executable: {reason}",
        )

    new_cfg = _clone(config)
    profiles = dict(new_cfg.llm_profiles)
    for key in profile_keys:
        profiles.pop(key, None)

    previous_keys = _llm_profile_storage_keys(config, previous_provider)
    for key in previous_keys:
        profiles.pop(key, None)
    if previous_provider:
        profiles[previous_provider] = LlmProviderProfile(
            model=str(config.llm.model or ""),
            api_key=str(config.llm.api_key or ""),
            api_key_env=str(config.llm.api_key_env or ""),
            api_key_env_pool=[],
            base_url=_stored_runtime_field(
                config, "llm.base_url", str(config.llm.base_url or "")
            ),
            proxy=_stored_runtime_field(config, "llm.proxy", str(config.llm.proxy or "")),
        )
    new_cfg.llm_profiles = profiles
    new_cfg.llm = LlmProviderConfig(
        provider=provider,
        model=model_id,
        api_key=str(profile.api_key or ""),
        api_key_env=str(profile.api_key_env or ""),
        base_url=str(profile.base_url or spec.default_base_url or ""),
        proxy=str(profile.proxy or ""),
    )
    _apply_primary_provider_router_policy(
        config,
        new_cfg,
        target_provider=provider,
        router_action=router_action,
    )

    # Move, rather than copy, secret provenance. A runtime-resolved env key
    # stays live in memory but remains absent from TOML after promotion or
    # demotion. Historical case-variant profile paths are removed as well.
    old_runtime = set(getattr(config, "_runtime_secret_paths", set()))
    old_explicit = set(getattr(config, "_explicit_secret_paths", set()))
    affected_paths = {"llm.api_key"}
    affected_paths.update(f"llm_profiles.{key}.api_key" for key in profile_keys)
    affected_paths.update(f"llm_profiles.{key}.api_key" for key in previous_keys)
    next_runtime = old_runtime - affected_paths
    next_explicit = old_explicit - affected_paths
    target_source_paths = {f"llm_profiles.{key}.api_key" for key in profile_keys}
    if target_source_paths & old_runtime:
        next_runtime.add("llm.api_key")
    if target_source_paths & old_explicit:
        next_explicit.add("llm.api_key")
    if previous_provider and "llm.api_key" in old_runtime:
        next_runtime.add(f"llm_profiles.{previous_provider}.api_key")
    if previous_provider and "llm.api_key" in old_explicit:
        next_explicit.add(f"llm_profiles.{previous_provider}.api_key")
    new_cfg._runtime_secret_paths = next_runtime
    new_cfg._explicit_secret_paths = next_explicit
    new_cfg.clear_runtime_override("llm.base_url")
    new_cfg.clear_runtime_override("llm.proxy")

    image_generation_change = apply_image_generation_intent(
        config,
        new_cfg,
        provider_id=provider,
        intent=image_generation_intent,
    )

    public_payload: dict[str, Any] = {
        "provider": provider,
        "model": new_cfg.llm.model,
        "previousProvider": previous_provider,
        "active": True,
        "routerBinding": new_cfg.squilla_router.preset_binding or "legacy",
    }
    if image_generation_change is not None:
        public_payload["capabilityChanges"] = {
            "imageGeneration": image_generation_change,
        }

    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=False,
        public_payload=public_payload,
    )


def remove_active_llm_profile(
    config: GatewayConfig,
    *,
    provider_id: str,
    replacement_provider_id: str,
    replacement_model: str | None = None,
    router_action: str | None = None,
    image_generation_intent: str | None = None,
) -> MutationResult:
    """Atomically promote a replacement and remove the previous primary.

    Both component mutations are pure.  The caller receives a final candidate
    only when activation and removal both succeed, so the RPC layer can persist
    the whole operation once and leave disk/runtime state untouched on failure.
    """

    provider = str(provider_id or "").strip().lower()
    active = str(config.llm.provider or "").strip().lower()
    if not provider or provider != active:
        raise LlmProfileRemovalError(
            "active_mismatch",
            f"provider {provider!r} is not the active LLM provider",
            details={"activeProvider": active},
        )

    replacement = str(replacement_provider_id or "").strip().lower()
    if not replacement or replacement == provider:
        raise LlmProfileRemovalError(
            "invalid_replacement",
            "replacement provider must differ from the active provider",
        )

    activated = activate_llm_profile(
        config,
        provider_id=replacement,
        model=replacement_model,
        router_action=router_action,
        image_generation_intent=image_generation_intent,
    )
    references = _profile_reference_labels(activated.config, provider)
    if references:
        raise LlmProfileRemovalError(
            "profile_referenced",
            f"LLM profile {provider!r} is still referenced by: {', '.join(references)}",
            details={"references": references},
        )

    removed = remove_llm_profile(activated.config, provider_id=provider)
    public_payload: dict[str, Any] = {
        "removedProvider": provider,
        "removed": True,
        "activeProvider": replacement,
        "activeModel": removed.config.llm.model,
    }
    capability_changes = activated.public_payload.get("capabilityChanges")
    if isinstance(capability_changes, dict):
        public_payload["capabilityChanges"] = capability_changes
    return MutationResult(
        config=removed.config,
        changed=activated.changed or removed.changed,
        restart_required=activated.restart_required or removed.restart_required,
        warnings=[*activated.warnings, *removed.warnings],
        public_payload=public_payload,
    )


def _channel_entries_as_dicts(cfg: GatewayConfig) -> list[dict[str, Any]]:
    return [e.model_dump(mode="python") for e in cfg.channels.channels]


def list_channel_entries(config: GatewayConfig) -> list[dict[str, Any]]:
    return [redact_channel_entry(d.get("type", ""), d) for d in _channel_entries_as_dicts(config)]


class ChannelValidationError(ValueError):
    """A channel-entry validation failure carrying per-field detail.

    Behaves like the plain ``ValueError`` callers already expect (the string
    message is unchanged) while additionally exposing ``field_errors`` so the
    RPC layer can return a structured envelope instead of only a joined string.
    """

    def __init__(self, message: str, field_errors: list[dict[str, str]]) -> None:
        super().__init__(message)
        self.field_errors = field_errors


def _channel_validation_field_errors(exc: ValidationError) -> list[dict[str, str]]:
    """Per-field ``{field, message}`` list; never echoes input values."""
    out: list[dict[str, str]] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        loc = ".".join(str(item) for item in error.get("loc", ()) or ())
        msg = str(error.get("msg") or "invalid value")
        out.append({"field": loc, "message": redact_error_text(msg, max_len=200)})
    return out


def _format_channel_validation_error(exc: ValidationError) -> str:
    """Render a channel-entry ValidationError as a field-naming summary.

    Never echoes pydantic's ``input_value`` dump: channel payloads carry
    credentials (bot tokens, app secrets) and this message surfaces on
    stderr and RPC error responses. Only field paths and validator messages
    are included, with the free-text redactor as a final guard.
    """
    parts: list[str] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        loc = ".".join(str(item) for item in error.get("loc", ()) or ())
        msg = str(error.get("msg") or "invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    detail = "; ".join(parts) or "invalid value"
    return f"invalid channel entry: {redact_error_text(detail, max_len=500)}"


def _require_non_blank_secret_fields(type_name: str, entry: Mapping[str, Any]) -> None:
    """Reject blank or sentinel-valued credential fields at mutation time.

    An empty or whitespace-only secret (e.g. ``--field token=``) would
    otherwise persist cleanly and only fail much later at gateway start.
    Fields gated by ``show_when`` are checked only when their condition
    matches the normalized entry. The literal ``'***'`` redaction sentinel is
    rejected for every secret field: it can only reach this point when a
    client echoed a redacted payload for an entry with no stored value to
    keep, and persisting it would overwrite a credential with asterisks.
    """
    from opensquilla.onboarding.channel_specs import get_channel_setup_spec

    try:
        spec = get_channel_setup_spec(type_name)
    except KeyError:
        return
    for field_spec in spec.fields:
        if not field_spec.secret:
            continue
        value = entry.get(field_spec.name)
        if isinstance(value, str) and value.strip() == REDACTED_PLACEHOLDER:
            raise ValueError(
                f"channel field {field_spec.name!r} looks redacted ({REDACTED_PLACEHOLDER!r}); "
                "provide the real value or leave it blank to keep the stored one"
            )
        if not field_spec.required:
            continue
        if field_spec.show_when and not all(
            str(entry.get(key, "")) == str(expected)
            for key, expected in field_spec.show_when.items()
        ):
            continue
        if value is None or not str(value).strip():
            raise ValueError(
                f"channel field {field_spec.name!r} requires a non-empty value"
            )


def validate_channel_entry(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("channel entry payload must be a dict")
    type_name = payload.get("type")
    if not isinstance(type_name, str) or not type_name:
        raise ValueError("channel entry requires non-empty 'type'")
    if type_name not in discover_all():
        raise ValueError(f"unknown channel type: {type_name!r}")
    full = {"agent_id": "main", "enabled": True, **payload}
    try:
        entry = parse_channel_entry(full)
    except ValidationError as exc:
        raise ChannelValidationError(
            _format_channel_validation_error(exc),
            _channel_validation_field_errors(exc),
        ) from exc
    normalized = entry.model_dump(mode="python")
    _require_non_blank_secret_fields(type_name, normalized)
    if (
        type_name == "slack"
        and getattr(entry, "connection_mode", "webhook") == "webhook"
        and not str(getattr(entry, "signing_secret", "") or "").strip()
    ):
        raise ValueError("slack webhook channels require signing_secret")
    return normalized


def upsert_channel(
    config: GatewayConfig,
    *,
    entry_payload: dict[str, Any],
) -> MutationResult:
    merged = _merge_with_existing_secrets(config, entry_payload)
    normalized = validate_channel_entry(merged)
    name = normalized["name"]
    new_cfg = _clone(config)
    raw = _channel_entries_as_dicts(new_cfg)
    replaced = False
    for idx, existing in enumerate(raw):
        if existing.get("name") == name:
            raw[idx] = normalized
            replaced = True
            break
    if not replaced:
        raw.append(normalized)
    new_cfg.channels = ChannelsConfig.model_validate({"channels": raw})

    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=True,
        warnings=[],
        public_payload=redact_channel_entry(normalized["type"], normalized),
    )


def _merge_with_existing_secrets(
    config: GatewayConfig, payload: dict[str, Any]
) -> dict[str, Any]:
    """Mirror upsert_llm_provider: blank secret in payload = keep current.

    Only secret fields are auto-preserved here so that re-adding an entry
    by name does not require re-typing credentials. Non-secret partial
    updates belong to the edit path, which seeds the full existing entry
    in the CLI before calling upsert.
    """
    from opensquilla.onboarding.channel_specs import get_channel_setup_spec

    type_name = payload.get("type")
    name = payload.get("name")
    if not isinstance(type_name, str) or not isinstance(name, str):
        return dict(payload)
    try:
        spec = get_channel_setup_spec(type_name)
    except KeyError:
        return dict(payload)
    existing = next(
        (
            e.model_dump(mode="python")
            for e in config.channels.channels
            if e.name == name and e.type == type_name
        ),
        None,
    )
    if existing is None:
        return dict(payload)
    merged = dict(payload)
    for f in spec.fields:
        if not f.secret:
            continue
        provided = merged.get(f.name)
        text = provided if isinstance(provided, str) else None
        blank = provided is None or (text is not None and not text.strip())
        # The '***' redaction sentinel is what channels.get / probe echo for a
        # stored secret; a client round-tripping that payload means "keep the
        # current value", never "my token is three asterisks". Enforced here,
        # server-side, so every RPC/CLI client gets the same trust boundary
        # (the Web UI scrub is defense in depth only).
        redacted = text is not None and text.strip() == REDACTED_PLACEHOLDER
        if (blank or redacted) and existing.get(f.name):
            merged[f.name] = existing[f.name]
    return merged


def merge_channel_entry_secrets(
    config: GatewayConfig, payload: dict[str, Any]
) -> dict[str, Any]:
    """Public wrapper over the blank-secret keep-current merge.

    Lets validation-only surfaces (gateway ``onboarding.channel.probe``)
    resolve blank secrets against the stored entry exactly the way
    ``upsert_channel`` does, so a probe of a keep-current payload does not
    hard-fail on the non-blank-secret requirement that the subsequent upsert
    would satisfy via the merge.
    """
    return _merge_with_existing_secrets(config, payload)


def remove_channel(
    config: GatewayConfig,
    *,
    name: str,
) -> MutationResult:
    new_cfg = _clone(config)
    raw = _channel_entries_as_dicts(new_cfg)
    remaining = [e for e in raw if e.get("name") != name]
    if len(remaining) == len(raw):
        raise KeyError(f"no channel named {name!r}")
    new_cfg.channels = ChannelsConfig.model_validate({"channels": remaining})
    # Removal withdraws admin standing too (mirroring pairing revoke): a
    # dormant channel_admin_senders entry would otherwise silently re-arm for
    # any future channel created under the same name.
    admin_senders = getattr(new_cfg, "channel_admin_senders", None)
    if isinstance(admin_senders, dict) and name in admin_senders:
        new_cfg.channel_admin_senders = {
            key: value for key, value in admin_senders.items() if key != name
        }
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=True,
        public_payload={"name": name, "removed": True},
    )


def set_channel_enabled(
    config: GatewayConfig,
    *,
    name: str,
    enabled: bool,
) -> MutationResult:
    new_cfg = _clone(config)
    raw = _channel_entries_as_dicts(new_cfg)
    found = False
    for entry in raw:
        if entry.get("name") == name:
            entry["enabled"] = bool(enabled)
            found = True
            break
    if not found:
        raise KeyError(f"no channel named {name!r}")
    new_cfg.channels = ChannelsConfig.model_validate({"channels": raw})
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=True,
        public_payload={"name": name, "enabled": bool(enabled)},
    )
