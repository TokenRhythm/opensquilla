"""Derive a structured OnboardingStatus from a GatewayConfig.

The per-section truth lives in :mod:`opensquilla.onboarding.section_status`;
this module composes those verifiers, computes the legacy boolean view
required by WebUI RPC and ``next_steps``, and exposes ``llm_source`` /
``image_generation_*`` annotations that the CLI status renderers need.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from opensquilla.gateway.config import (
    GatewayConfig,
    LlmProviderProfile,
)
from opensquilla.gateway.llm_runtime import resolve_llm_credential
from opensquilla.onboarding.audio_specs import get_audio_provider_setup_spec
from opensquilla.onboarding.config_store import default_config_path
from opensquilla.onboarding.image_generation_specs import (
    get_image_generation_provider_setup_spec,
)
from opensquilla.onboarding.image_generation_state import (
    resolve_image_generation_state,
)
from opensquilla.onboarding.provider_specs import get_provider_setup_spec
from opensquilla.onboarding.search_specs import get_search_provider_setup_spec
from opensquilla.onboarding.section_status import (
    FIRST_RUN_REQUIRED_SECTIONS,
    SectionStatus,
    _configured_image_generation_provider_ids,
    _image_generation_effective_endpoint,
    _image_generation_endpoint_conflict_provider,
    _image_generation_endpoint_is_valid,
    _image_generation_has_invalid_model_reference,
    _image_generation_llm_key_reusable,
    audio_section_status,
    channels_section_status,
    ensemble_section_status,
    image_generation_section_status,
    llm_section_status,
    memory_embedding_section_status,
    router_section_status,
    search_section_status,
    section_verifiers,
)
from opensquilla.onboarding.section_status import (
    needs_onboarding as _needs_onboarding,
)
from opensquilla.provider.environment import environment_value
from opensquilla.provider.image_generation_credentials import (
    resolve_image_generation_credential,
)
from opensquilla.provider.preset_registry import get_preset
from opensquilla.router_tiers import (
    LEGACY_OPENROUTER_MODEL_OPTIONS,
    ROUTER_DYNAMIC_SELECTION_MODE,
    STATIC_B5_SELECTION_MODE_PROVIDERS,
    TierConfig,
    effective_ensemble_selection_mode,
    router_dynamic_tier_members_active,
    router_tier_provider_roles,
    tier_provider_role,
)


@dataclass(frozen=True)
class OnboardingStatus:
    config_path: str | None
    has_config: bool
    llm_configured: bool
    llm_source: str
    llm_env_key: str
    search_configured: bool
    search_provider: str
    search_source: str
    search_env_key: str
    image_generation_configured: bool
    image_generation_enabled: bool
    image_generation_source: str
    image_generation_provider: str
    image_generation_primary: str
    image_generation_env_key: str
    audio_configured: bool
    audio_enabled: bool
    audio_source: str
    audio_provider: str
    audio_env_key: str
    memory_embedding_configured: bool
    memory_embedding_provider: str
    memory_embedding_source: str
    memory_embedding_env_key: str
    channel_count: int
    channels_configured: bool
    needs_onboarding: bool
    llm_credential_status: dict[str, object] = field(default_factory=dict)
    llm_profile_status: tuple[dict[str, object], ...] = ()
    ensemble_credential_status: tuple[dict[str, object], ...] = ()
    sections: dict[str, SectionStatus] = field(default_factory=dict)
    section_details: dict[str, dict[str, object]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    # Additive read-side metadata stays optional so integrations that construct
    # this public dataclass with the pre-feature positional shape keep working.
    image_generation_state: dict[str, object] = field(default_factory=dict)


_SECTION_LABELS: dict[str, str] = {
    "llm": "Provider",
    "router": "Router",
    "ensemble": "LLM ensemble",
    "search": "Web search",
    "channels": "Channels",
    "image_generation": "Image generation",
    "audio": "Voice audio",
    "memory_embedding": "Memory embedding",
}


def _section_details(
    sections: dict[str, SectionStatus],
    detail_text: dict[str, str] | None = None,
    runtime_blocking: set[str] | None = None,
) -> dict[str, dict[str, object]]:
    details: dict[str, dict[str, object]] = {}
    for name, state in sections.items():
        required = name in FIRST_RUN_REQUIRED_SECTIONS
        action_required = state not in (SectionStatus.OK, SectionStatus.OPTIONAL)
        blocking = (required and action_required) or (
            action_required and name in (runtime_blocking or set())
        )
        details[name] = {
            "label": _SECTION_LABELS.get(name, name.replace("_", " ").title()),
            "status": state.value,
            "required": required,
            "optional": not required,
            "blocking": blocking,
            "actionRequired": action_required,
        }
        if detail_text and detail_text.get(name):
            details[name]["detail"] = detail_text[name]
    return details


def _source_detail(source: str, env_key: str = "") -> str:
    if source == "explicit":
        return "stored key"
    if source == "env":
        return f"env key visible: {env_key}" if env_key else "env key visible"
    if source == "missing_env":
        return f"env key not visible: {env_key}" if env_key else "env key not visible"
    if source == "not_required":
        return "no key required"
    if source == "unsupported":
        return "registered but not runtime-supported"
    return ""


def _with_provider(provider: str, detail: str) -> str:
    if provider and detail:
        return f"{provider} ({detail})"
    return provider or detail


def _router_detail(cfg: GatewayConfig, llm_source: str) -> str:
    router = getattr(cfg, "squilla_router", None)
    if router is None or not bool(getattr(router, "enabled", False)):
        return "disabled"
    llm = getattr(cfg, "llm", None)
    if llm_source == "none" or not getattr(llm, "provider", ""):
        return "uses SquillaRouter after provider setup"
    profile = str(getattr(router, "tier_profile", "") or "").strip()
    if profile:
        return f"SquillaRouter profile: {profile}"
    default_tier = str(getattr(router, "default_tier", "") or "c1").strip()
    return f"SquillaRouter default tier: {default_tier}"


_ENSEMBLE_ONBOARDING_STATUS_KEYS = (
    "enabled",
    "selectionMode",
    "runtimeStatus",
    "configurationReady",
    "blockedReason",
    "proposerCount",
    "proposerCountRange",
    "aggregatorCount",
    "perTurnCallCount",
    "perTurnCallCountRange",
    "memberProviders",
    "configuredAllFailedPolicy",
    "effectiveAllFailedPolicy",
    "policyDeprecated",
    "configuredMinSuccessfulProposers",
    "effectiveMinSuccessfulProposers",
    "configuredProposerMaxRetries",
    "effectiveProposerMaxRetries",
    "proposerMaxRetriesSource",
    "fixedFallbackReady",
    "fixedFallbackBlockedReason",
    "fixedFallbackProvider",
    "fixedFallbackModel",
    "blockedTierCandidates",
)


def _ensemble_onboarding_status(cfg: GatewayConfig) -> dict[str, object]:
    """Project global Ensemble state onto the frozen onboarding wire shape.

    Tier-managed fusion is part of the router ladder and is diagnosed by the
    doctor/router surfaces.  It must not make the global Ensemble toggle look
    enabled or disable the router editor in older WebUI clients.
    """
    globally_enabled = bool(getattr(getattr(cfg, "llm_ensemble", None), "enabled", False))
    configured_policy = str(
        getattr(getattr(cfg, "llm_ensemble", None), "all_failed_policy", "fallback_single")
        or "fallback_single"
    ).strip()
    configured_min = max(
        1,
        int(getattr(getattr(cfg, "llm_ensemble", None), "min_successful_proposers", 1) or 1),
    )
    configured_retries = max(
        0,
        int(getattr(getattr(cfg, "llm_ensemble", None), "proposer_max_retries", 0) or 0),
    )
    ensemble_fields_set = set(
        getattr(getattr(cfg, "llm_ensemble", None), "model_fields_set", set())
    )
    c3_config = TierConfig.from_value(
        (getattr(getattr(cfg, "squilla_router", None), "tiers", {}) or {}).get("c3")
    )
    c3_default_retries = bool(
        not globally_enabled
        and bool(getattr(getattr(cfg, "squilla_router", None), "enabled", False))
        and c3_config.ensemble_enabled is True
        and "proposer_max_retries" not in ensemble_fields_set
    )
    if not globally_enabled:
        llm = getattr(cfg, "llm", None)
        selection_mode = str(
            getattr(getattr(cfg, "llm_ensemble", None), "selection_mode", "") or ""
        )
        runtime = {
            "enabled": False,
            "selectionMode": selection_mode,
            "runtimeStatus": "disabled",
            "configurationReady": None,
            "blockedReason": None,
            "proposerCount": 0,
            "proposerCountRange": None,
            "aggregatorCount": 0,
            "perTurnCallCount": 0,
            "perTurnCallCountRange": None,
            "memberProviders": [],
            "configuredAllFailedPolicy": configured_policy,
            "effectiveAllFailedPolicy": configured_policy,
            "policyDeprecated": False,
            "configuredMinSuccessfulProposers": configured_min,
            "effectiveMinSuccessfulProposers": configured_min,
            "configuredProposerMaxRetries": configured_retries,
            "effectiveProposerMaxRetries": 1 if c3_default_retries else configured_retries,
            "proposerMaxRetriesSource": (
                "c3_default" if c3_default_retries else "configured"
            ),
            # The global card is intentionally a top-level-toggle projection.
            # When it is disabled, readiness is not evaluated here; active
            # tier-local plans expose their truthful fallback status on the
            # Router card instead.
            "fixedFallbackReady": None,
            "fixedFallbackBlockedReason": None,
            "fixedFallbackProvider": str(getattr(llm, "provider", "") or ""),
            "fixedFallbackModel": str(getattr(llm, "model", "") or ""),
            "blockedTierCandidates": [],
        }
    else:
        # Runtime readiness may resolve several provider credentials. Avoid
        # that work (and its diagnostic noise) when this global surface is
        # disabled; tier-managed fusion is projected by router/doctor status.
        from opensquilla.provider.ensemble import ensemble_runtime_status

        runtime = ensemble_runtime_status(cfg)
        runtime.setdefault("configuredAllFailedPolicy", configured_policy)
        runtime.setdefault("effectiveAllFailedPolicy", configured_policy)
        runtime.setdefault("policyDeprecated", False)
    return {key: runtime.get(key) for key in _ENSEMBLE_ONBOARDING_STATUS_KEYS}


_TIER_ENSEMBLE_STATUS_KEYS = (
    "selectionMode",
    "activationTiers",
    "tierSelectionModes",
    "runtimeStatus",
    "configurationReady",
    "blockedReason",
    "blockedTierCandidates",
    "proposerCount",
    "proposerCountRange",
    "fixedFallbackReady",
    "fixedFallbackBlockedReason",
    "configuredAllFailedPolicy",
    "effectiveAllFailedPolicy",
    "configuredMinSuccessfulProposers",
    "effectiveMinSuccessfulProposers",
    "configuredProposerMaxRetries",
    "effectiveProposerMaxRetries",
    "proposerMaxRetriesSource",
)


def _tier_ensemble_onboarding_statuses(
    cfg: GatewayConfig,
) -> dict[str, dict[str, object]]:
    """Project each active tier-local fusion plan onto the Router card.

    The global Ensemble card intentionally mirrors only the top-level toggle
    for older clients.  Per-tier rows prevent mixed legacy profiles from
    borrowing whichever selection mode happens to appear first in a mapping.
    A globally enabled plan is not tier-local, but retained legacy overrides
    remain visible because runtime still honors them for upgrade compatibility.
    """

    from opensquilla.provider.ensemble import tier_ensemble_runtime_statuses

    statuses: dict[str, dict[str, object]] = {}
    for tier, runtime in tier_ensemble_runtime_statuses(cfg).items():
        statuses[tier] = {
            key: runtime.get(key)
            for key in _TIER_ENSEMBLE_STATUS_KEYS
        }
    return statuses


def _ensemble_detail(cfg: GatewayConfig) -> str:
    runtime = _ensemble_onboarding_status(cfg)
    if not runtime["enabled"]:
        return "disabled"
    mode = str(runtime["selectionMode"])
    proposer_count = runtime.get("proposerCount")
    if proposer_count is None:
        raw_proposer_range = runtime.get("proposerCountRange")
        proposer_range = (
            raw_proposer_range
            if isinstance(raw_proposer_range, list | tuple)
            else []
        )
        count_text = (
            f"{proposer_range[0]}-{proposer_range[1]} proposers"
            if len(proposer_range) == 2
            else "dynamic proposers"
        )
    else:
        count_text = f"{proposer_count} proposers"
    return f"selection mode: {mode} ({count_text})"


def _candidate_field(candidate: object, field_name: str) -> object:
    if isinstance(candidate, dict):
        return candidate.get(field_name)
    return getattr(candidate, field_name, None)


def _ensemble_candidate_provider_ids(cfg: GatewayConfig) -> list[str]:
    ensemble = getattr(cfg, "llm_ensemble", None)
    if ensemble is None or not bool(getattr(ensemble, "enabled", False)):
        return []

    provider_ids: list[str] = []
    seen: set[str] = set()

    def add(provider: object) -> None:
        provider_id = str(provider or "").strip().lower()
        if provider_id and provider_id not in seen:
            seen.add(provider_id)
            provider_ids.append(provider_id)

    llm = getattr(cfg, "llm", None)
    add(getattr(llm, "provider", ""))

    selection_mode = str(getattr(ensemble, "selection_mode", "") or "")
    add(STATIC_B5_SELECTION_MODE_PROVIDERS.get(selection_mode, ""))

    router = getattr(cfg, "squilla_router", None)
    tiers = getattr(router, "tiers", {}) or {}
    if selection_mode == ROUTER_DYNAMIC_SELECTION_MODE and isinstance(tiers, dict):
        for tier_cfg in tiers.values():
            if isinstance(tier_cfg, dict):
                add(tier_cfg.get("provider") or getattr(llm, "provider", ""))

    for candidate in getattr(ensemble, "candidates", []) or []:
        if _candidate_field(candidate, "enabled") is False:
            continue
        add(_candidate_field(candidate, "provider"))

    model_options = list(getattr(ensemble, "model_options", []) or [])
    if tuple(model_options) == tuple(LEGACY_OPENROUTER_MODEL_OPTIONS):
        model_options = []
    for model in model_options:
        model_s = str(model or "").strip()
        if not model_s:
            continue
        add("openrouter" if "/" in model_s else getattr(llm, "provider", ""))

    return provider_ids


def _llm_provider_credential_status(
    cfg: GatewayConfig,
    provider_id: str,
) -> dict[str, object]:
    provider = str(provider_id or "").strip().lower()
    try:
        spec = get_provider_setup_spec(provider)
    except KeyError:
        return {"provider": provider, "available": False, "source": "none", "envKey": ""}

    env_key = str(getattr(spec, "env_key", "") or "").strip()
    if not spec.runtime_supported:
        # Keep this aligned with ``_llm_source``: a registered but
        # runtime-unsupported provider must never report a satisfied
        # credential state ("not_required") while llmSource says
        # "unsupported" — nothing can run against it.
        return {
            "provider": provider,
            "available": False,
            "source": "unsupported",
            "envKey": env_key,
        }
    if not spec.accepts_api_key:
        return {
            "provider": provider,
            "available": True,
            "source": "not_required",
            "envKey": env_key,
        }

    llm = getattr(cfg, "llm", None)
    current_provider = str(getattr(llm, "provider", "") or "").strip().lower()
    if provider == current_provider:
        configured_env_key = str(getattr(llm, "api_key_env", "") or "").strip()
        settings_env_key = environment_value("OPENSQUILLA_LLM_API_KEY_ENV").strip()
        credential = resolve_llm_credential(
            cfg,
            registry_env_key=env_key,
            include_runtime_cache=False,
        )
        env_key = credential.env_name
        if credential.source == "explicit":
            return {
                "provider": provider,
                "available": True,
                "source": "explicit",
                "envKey": env_key,
            }
        if credential.source == "env":
            return {
                "provider": provider,
                "available": True,
                "source": "env",
                "envKey": env_key,
            }
        if not spec.requires_api_key and not (configured_env_key or settings_env_key):
            return {
                "provider": provider,
                "available": True,
                "source": "not_required",
                "envKey": env_key,
            }
        return {
            "provider": provider,
            "available": False,
            "source": "missing_env" if env_key else "none",
            "envKey": env_key,
        }

    if env_key and os.environ.get(env_key):
        return {
            "provider": provider,
            "available": True,
            "source": "env",
            "envKey": env_key,
        }
    if not spec.requires_api_key:
        return {
            "provider": provider,
            "available": True,
            "source": "not_required",
            "envKey": env_key,
        }
    return {
        "provider": provider,
        "available": False,
        "source": "missing_env" if env_key else "none",
        "envKey": env_key,
    }


def _mask_credential(value: str) -> str:
    secret = str(value or "")
    if not secret:
        return ""
    if len(secret) <= 4:
        return "*" * len(secret)
    return f"{'*' * (len(secret) - 4)}{secret[-4:]}"


def _llm_credential_status(cfg: GatewayConfig) -> dict[str, object]:
    llm = getattr(cfg, "llm", None)
    provider = str(getattr(llm, "provider", "") or "").strip().lower()
    if not provider:
        return {
            "provider": "",
            "available": False,
            "source": "none",
            "envKey": "",
            "masked": "",
            "revealAllowed": False,
        }

    try:
        spec = get_provider_setup_spec(provider)
    except KeyError:
        return {
            "provider": provider,
            "available": False,
            "source": "none",
            "envKey": "",
            "masked": "",
            "revealAllowed": False,
        }

    env_key = str(getattr(spec, "env_key", "") or "").strip()
    configured_env_key = str(getattr(llm, "api_key_env", "") or "").strip()
    settings_env_key = environment_value("OPENSQUILLA_LLM_API_KEY_ENV").strip()
    credential = resolve_llm_credential(
        cfg,
        registry_env_key=env_key,
        include_runtime_cache=False,
    )
    resolved_env_key = credential.env_name

    if not spec.runtime_supported:
        # Mirror ``_llm_source``'s "unsupported" enumerant so the status
        # payload is internally consistent (llmSource vs
        # llmCredentialStatus.source) for the same provider.
        return {
            "provider": provider,
            "available": False,
            "source": "unsupported",
            "envKey": resolved_env_key,
            "masked": "",
            "revealAllowed": False,
        }

    if not spec.accepts_api_key:
        return {
            "provider": provider,
            "available": True,
            "source": "not_required",
            "envKey": resolved_env_key,
            "masked": "",
            "revealAllowed": False,
        }

    if credential.source == "explicit":
        return {
            "provider": provider,
            "available": True,
            "source": "explicit",
            "envKey": resolved_env_key,
            "masked": _mask_credential(credential.api_key),
            "revealAllowed": False,
        }

    if credential.source == "env":
        return {
            "provider": provider,
            "available": True,
            "source": "env",
            "envKey": resolved_env_key,
            "masked": _mask_credential(credential.api_key),
            "revealAllowed": False,
        }

    if resolved_env_key:
        if not spec.requires_api_key and not (configured_env_key or settings_env_key):
            return {
                "provider": provider,
                "available": True,
                "source": "not_required",
                "envKey": resolved_env_key,
                "masked": "",
                "revealAllowed": False,
            }
        return {
            "provider": provider,
            "available": False,
            "source": "missing_env",
            "envKey": resolved_env_key,
            "masked": "",
            "revealAllowed": False,
        }

    if not spec.requires_api_key:
        return {
            "provider": provider,
            "available": True,
            "source": "not_required",
            "envKey": resolved_env_key,
            "masked": "",
            "revealAllowed": False,
        }

    return {
        "provider": provider,
        "available": False,
        "source": "none",
        "envKey": "",
        "masked": "",
        "revealAllowed": False,
    }


def _llm_profile_for(cfg: GatewayConfig, provider_id: str) -> LlmProviderProfile | None:
    """Return a profile by normalized provider id without mutating old config."""
    provider = str(provider_id or "").strip().lower()
    profiles = cfg.llm_profiles
    profile = profiles.get(provider)
    if profile is not None:
        return profile
    for key, candidate in profiles.items():
        if str(key or "").strip().lower() == provider:
            return candidate
    return None


def _ensemble_credential_status(
    cfg: GatewayConfig,
    deployment_statuses: tuple[dict[str, object], ...] | None = None,
) -> tuple[dict[str, object], ...]:
    """Project deployment readiness onto the legacy ensemble status shape.

    The legacy status is still consumed by older WebUI builds.  Resolve it
    from the same profile-aware deployment rows as ``llmProfileStatus`` so a
    profile key, key pool, or profile env cannot be reported as missing here
    while the runtime considers the deployment ready.
    """
    if deployment_statuses is None:
        deployment_statuses = _llm_profile_status(cfg)
    by_provider = {
        str(row.get("provider") or "").strip().lower(): row
        for row in deployment_statuses
    }
    source_map = {
        "profile": "explicit",
        "member": "explicit",
        "profile_pool": "env",
        "profile_pool_env": "env",
        "profile_env": "env",
        "registry_env": "env",
        "member_env": "env",
        "keyless": "not_required",
    }
    rows: list[dict[str, object]] = []
    for provider in _ensemble_candidate_provider_ids(cfg):
        baseline = _llm_provider_credential_status(cfg, provider)
        deployment = by_provider.get(provider)
        if deployment is None:
            rows.append(baseline)
            continue

        credential_source = str(deployment.get("credentialSource") or "")
        source = source_map.get(credential_source, str(baseline["source"]))
        if str(deployment.get("reason") or "") == "runtime_unsupported":
            source = "unsupported"

        env_key = str(deployment.get("credentialEnv") or "").strip()
        if not env_key:
            profile = _llm_profile_for(cfg, provider)
            pool = list(getattr(profile, "api_key_env_pool", None) or [])
            profile_env = str(getattr(profile, "api_key_env", "") or "").strip()
            env_key = next(
                (str(name).strip() for name in pool if str(name).strip()),
                profile_env or str(baseline["envKey"]),
            )

        rows.append(
            {
                "provider": provider,
                "available": bool(deployment.get("ready")),
                "source": source,
                "envKey": env_key,
            }
        )
    return tuple(rows)


def _provider_deployment_models(cfg: GatewayConfig) -> dict[str, str]:
    """Collect one representative model for every configured/referenced provider."""
    models: dict[str, str] = {}

    def add(provider: object, model: object) -> None:
        provider_id = str(provider or "").strip().lower()
        model_id = str(model or "").strip()
        if provider_id and model_id and provider_id not in models:
            models[provider_id] = model_id

    llm = getattr(cfg, "llm", None)
    add(getattr(llm, "provider", ""), getattr(llm, "model", ""))
    for provider, profile in (getattr(cfg, "llm_profiles", None) or {}).items():
        add(provider, getattr(profile, "model", ""))
    tiers = getattr(getattr(cfg, "squilla_router", None), "tiers", {}) or {}
    if isinstance(tiers, dict):
        for tier in tiers.values():
            if isinstance(tier, dict):
                add(tier.get("provider"), tier.get("model"))
    ensemble = getattr(cfg, "llm_ensemble", None)
    for candidate in getattr(ensemble, "candidates", None) or []:
        if _candidate_field(candidate, "enabled") is False:
            continue
        add(_candidate_field(candidate, "provider"), _candidate_field(candidate, "model"))
    return models


def _profile_direct_model(cfg: GatewayConfig, provider: str) -> str:
    profile = _llm_profile_for(cfg, provider)
    saved = str(getattr(profile, "model", "") or "").strip()
    if saved:
        return saved
    try:
        return str(get_provider_setup_spec(provider).default_direct_model or "").strip()
    except KeyError:
        return ""


def _llm_profile_status(
    cfg: GatewayConfig,
    *,
    probe_history: dict[str, dict[str, object]] | None = None,
) -> tuple[dict[str, object], ...]:
    """Resolve all configured/referenced deployments into a secret-free status view."""
    from opensquilla.gateway.llm_runtime import (
        NoCredentialsAvailable,
        profile_credential_pools,
        resolve_llm_runtime_config,
    )
    from opensquilla.provider.deployment import (
        CredentialPoolExhaustedError,
        resolve_provider_deployment,
    )
    from opensquilla.provider.selector import ProviderConfig

    def peek_profile_credential(
        provider_id: str,
        pool_names: list[str],
        _session_key: str,
    ) -> object | None:
        """Inspect readiness without importing the engine execution layer."""
        try:
            return profile_credential_pools().peek_available(provider_id, pool_names)
        except NoCredentialsAvailable as exc:
            raise CredentialPoolExhaustedError from exc

    models = _provider_deployment_models(cfg)
    provider_ids = set(models)
    provider_ids.update(
        str(provider or "").strip().lower()
        for provider in (getattr(cfg, "llm_profiles", None) or {})
        if str(provider or "").strip()
    )
    provider_ids.update(_ensemble_candidate_provider_ids(cfg))
    active_provider = str(getattr(cfg.llm, "provider", "") or "").strip().lower()
    if active_provider:
        provider_ids.add(active_provider)

    inherited: ProviderConfig | None = None
    if active_provider:
        scratch = cfg.model_copy(deep=True)
        runtime = resolve_llm_runtime_config(scratch)
        inherited = ProviderConfig(
            provider=active_provider,
            model=str(getattr(runtime, "model", "") or getattr(cfg.llm, "model", "")),
            api_key=str(getattr(runtime, "api_key", "") or ""),
            base_url=str(getattr(runtime, "base_url", "") or ""),
            proxy=str(getattr(runtime, "proxy", "") or ""),
            provider_routing=dict(getattr(runtime, "provider_routing", {}) or {}),
            replay_provider_state=bool(
                getattr(runtime, "replay_provider_state", True)
            ),
        )

    ordered = ([active_provider] if active_provider else []) + sorted(
        provider for provider in provider_ids if provider != active_provider
    )
    statuses: list[dict[str, object]] = []
    fallback_model = str(getattr(cfg.llm, "model", "") or "profile-status")
    for provider in ordered:
        resolution = resolve_provider_deployment(
            cfg,
            provider,
            models.get(provider) or fallback_model,
            inherited_provider_config=inherited,
            session_key="onboarding-status",
            credential_pool_acquirer=peek_profile_credential,
        )
        profile = _llm_profile_for(cfg, provider)
        if provider == active_provider:
            primary_eligible = False
            primary_block_reason = "already_active"
        elif profile is None:
            primary_eligible = False
            primary_block_reason = "profile_not_found"
        elif list(getattr(profile, "api_key_env_pool", None) or []):
            primary_eligible = False
            primary_block_reason = "primary_pool_unsupported"
        elif not _profile_direct_model(cfg, provider):
            primary_eligible = False
            primary_block_reason = "missing_model"
        elif not resolution.ready:
            primary_eligible = False
            primary_block_reason = resolution.reason or "not_executable"
        else:
            primary_eligible = True
            primary_block_reason = ""
        row: dict[str, object] = {
            "provider": resolution.provider,
            "ready": resolution.ready,
            "credentialSource": resolution.credential_source,
            "credentialEnv": resolution.credential_env,
            "endpointSource": resolution.endpoint_source,
            "proxySource": resolution.proxy_source,
            "reason": resolution.reason,
            "primaryEligible": primary_eligible,
            "primaryBlockReason": primary_block_reason,
        }
        if probe_history is not None:
            from opensquilla.onboarding.probe_history import (
                last_probe_payload,
                saved_deployment_fingerprint,
            )

            last_probe = last_probe_payload(
                probe_history.get(provider),
                saved_deployment_fingerprint(cfg, provider),
            )
            if last_probe is not None:
                row["lastProbe"] = last_probe
        statuses.append(row)
    return tuple(statuses)


def _router_mode(cfg: GatewayConfig) -> str:
    """Compute the effective Router editing mode for compatibility clients.

    Explicit ownership wins over shape inference.  Shape inference remains
    only for legacy configs that predate ``preset_binding``:

    - ``disabled`` when the router is off;
    - ``openrouter-mix`` when enabled, provider is ``openrouter``, and no
      persisted ``tier_profile`` (the openrouter-only alias);
    - ``custom`` when enabled with no ``tier_profile`` on a non-openrouter
      provider (the provider-agnostic generalization);
    - ``recommended`` otherwise (a persisted legacy tier_profile).
    """
    router = getattr(cfg, "squilla_router", None)
    if router is None or not bool(getattr(router, "enabled", False)):
        return "disabled"
    provider = str(getattr(getattr(cfg, "llm", None), "provider", "") or "").strip().lower()
    binding = str(getattr(router, "preset_binding", "") or "").strip().lower()
    if binding == "custom":
        return "custom"
    if binding == "follow_primary":
        return "recommended"
    tier_profile = str(getattr(router, "tier_profile", "") or "").strip()
    if not tier_profile:
        return "openrouter-mix" if provider == "openrouter" else "custom"
    return "recommended"


def _router_binding(cfg: GatewayConfig) -> str:
    """Return explicit ownership or the conservative legacy sentinel.

    A disabled, sparse Router section has no authored ladder to preserve, but
    its settings model still materializes the built-in OpenRouter tiers.  Mark
    that one provenance-backed case as follow-primary so clients re-enable the
    current provider's managed preset instead of adopting those defaults as a
    custom cross-provider route.  Explicit historical tiers remain legacy.
    """

    router = getattr(cfg, "squilla_router", None)
    value = str(getattr(router, "preset_binding", "") or "").strip().lower()
    if value in {"follow_primary", "custom"}:
        return value
    fields_set = set(getattr(router, "model_fields_set", set()))
    provider = str(getattr(getattr(cfg, "llm", None), "provider", "") or "").strip().lower()
    if (
        router is not None
        and not bool(getattr(router, "enabled", False))
        and not fields_set.intersection({"tiers", "tier_profile", "preset_binding"})
        and get_preset(provider) is not None
    ):
        return "follow_primary"
    return "legacy"


def _router_provider_conflicts(cfg: GatewayConfig) -> tuple[str, ...]:
    """Secret-free foreign-provider summary relative to the active primary."""

    router = getattr(cfg, "squilla_router", None)
    if router is None or not bool(getattr(router, "enabled", False)):
        return ()
    if bool(getattr(router, "cross_provider_tiers", False)):
        return ()
    active = str(getattr(getattr(cfg, "llm", None), "provider", "") or "").strip().lower()
    conflicts: set[str] = set()
    tiers = getattr(router, "tiers", {}) or {}
    shared_selection_mode = effective_ensemble_selection_mode(cfg)
    ensemble_globally_enabled = bool(
        getattr(getattr(cfg, "llm_ensemble", None), "enabled", False)
    )
    dynamic_members_active = router_dynamic_tier_members_active(
        tiers if isinstance(tiers, dict) else {},
        shared_selection_mode=shared_selection_mode,
        ensemble_globally_enabled=ensemble_globally_enabled,
    )
    if isinstance(tiers, dict):
        for tier_name, tier in tiers.items():
            if not isinstance(tier, dict):
                continue
            provider_role = tier_provider_role(
                tier_name,
                tier,
                shared_selection_mode=shared_selection_mode,
                router_dynamic_members_active=dynamic_members_active,
                ensemble_globally_enabled=ensemble_globally_enabled,
            )
            if provider_role not in {"direct", "dynamic_member"}:
                continue
            provider = str(tier.get("provider") or "").strip().lower()
            if provider and provider != active:
                conflicts.add(provider)
    return tuple(sorted(conflicts))


def _llm_source(cfg: GatewayConfig, status: SectionStatus) -> tuple[str, str]:
    """Re-derive the legacy ``llm_source`` annotation alongside the verifier.

    The verifier collapses the source detail into a single enum so it stays
    composable with the other sections; this helper keeps the existing
    ``"explicit" / "env" / "missing_env" / "none"`` annotation alive for the
    CLI/WebUI renderers that already display it.
    """
    llm = cfg.llm
    if not llm.provider or not llm.model:
        return "none", ""
    try:
        spec = get_provider_setup_spec(llm.provider)
    except KeyError:
        return "none", ""
    if not spec.runtime_supported:
        # Registered but runtime-unsupported providers (e.g. coding-plan
        # stubs) are not "no key required": nothing can run against them,
        # so never report the credential state as satisfied.
        return "unsupported", ""
    if not spec.requires_api_key:
        return "not_required", ""
    if status is SectionStatus.OK and llm.api_key and (
        "llm.api_key" not in getattr(cfg, "_runtime_secret_paths", set())
    ):
        return "explicit", ""
    env_key = (getattr(llm, "api_key_env", "") or "").strip()
    if env_key and os.environ.get(env_key):
        return "env", env_key
    if env_key:
        return "missing_env", env_key
    if status is SectionStatus.OK:
        return "env", spec.env_key
    return "none", spec.env_key


def _search_annotations(
    cfg: GatewayConfig,
    status: SectionStatus,
) -> tuple[str, str, str]:
    provider = str(getattr(cfg, "search_provider", "") or "").strip()
    if not provider:
        return "", "none", ""
    try:
        spec = get_search_provider_setup_spec(provider)
    except KeyError:
        return provider, "none", ""
    if not spec.requires_api_key:
        return provider, "not_required", ""
    if getattr(cfg, "search_api_key", ""):
        return provider, "explicit", ""
    env_key = str(getattr(cfg, "search_api_key_env", "") or "").strip()
    if env_key and os.environ.get(env_key):
        return provider, "env", env_key
    if env_key:
        return provider, "missing_env", env_key
    if status is SectionStatus.OK:
        return provider, "env", spec.env_key
    return provider, "none", spec.env_key


def _image_generation_provider_config(cfg: GatewayConfig, provider_id: str) -> object | None:
    providers = getattr(getattr(cfg, "image_generation", None), "providers", None)
    return getattr(providers, provider_id, None) if providers is not None else None


def _image_generation_provider_source(
    cfg: GatewayConfig,
    provider_id: str,
) -> tuple[str, str]:
    try:
        spec = get_image_generation_provider_setup_spec(provider_id)
    except KeyError:
        return "", ""

    provider_cfg = _image_generation_provider_config(cfg, provider_id)
    endpoint = _image_generation_effective_endpoint(cfg, provider_id)
    if endpoint is None:
        return "", ""
    resolution = resolve_image_generation_credential(
        provider_id=provider_id,
        provider_config=provider_cfg,
        default_env_key=spec.env_key,
        default_base_url=endpoint[0],
        effective_base_url=(
            spec.default_base_url
            if str(getattr(cfg.image_generation, "binding", "custom") or "custom")
            == "follow_llm"
            else endpoint[1]
        ),
        gateway_config=cfg,
        model=str(getattr(cfg.image_generation, "primary", "") or "image-generation"),
        include_image_credentials=(
            str(getattr(cfg.image_generation, "binding", "custom") or "custom")
            != "follow_llm"
        ),
    )
    return (
        resolution.source if resolution.source != "none" else "",
        resolution.env_key or spec.env_key,
    )


def _image_generation_annotations(
    cfg: GatewayConfig,
    status: SectionStatus,
) -> tuple[str, str, str, str]:
    image_cfg = cfg.image_generation
    primary = getattr(image_cfg, "primary", "")
    if status is SectionStatus.OPTIONAL:
        return "none", "", primary, ""
    for provider_id in _configured_image_generation_provider_ids(cfg):
        source, env_key = _image_generation_provider_source(cfg, provider_id)
        if source:
            return source, provider_id, primary, env_key
        try:
            get_image_generation_provider_setup_spec(provider_id)
        except KeyError:
            return "none", provider_id, primary, ""
        if _image_generation_llm_key_reusable(cfg, provider_id) is False:
            return "none", provider_id, primary, env_key
    return "none", "", primary, ""


def _image_generation_detail(
    cfg: GatewayConfig,
    status: SectionStatus,
    source: str,
    provider: str,
    env_key: str,
) -> str:
    if status is SectionStatus.UNKNOWN and _image_generation_has_invalid_model_reference(cfg):
        return "invalid image provider/model reference"
    if status is SectionStatus.UNKNOWN and provider:
        try:
            get_image_generation_provider_setup_spec(provider)
        except KeyError:
            return _with_provider(provider, "unknown image provider")
    if status is SectionStatus.DEGRADED:
        for candidate_provider in _configured_image_generation_provider_ids(cfg):
            if _image_generation_endpoint_is_valid(cfg, candidate_provider) is False:
                return _with_provider(
                    candidate_provider,
                    "invalid image endpoint; use an absolute http:// or https:// URL",
                )
            conflicting_provider = _image_generation_endpoint_conflict_provider(
                cfg,
                candidate_provider,
            )
            if conflicting_provider is not None:
                return _with_provider(
                    candidate_provider,
                    "endpoint/provider mismatch: "
                    f"configured {conflicting_provider} official endpoint",
                )
            if _image_generation_llm_key_reusable(cfg, candidate_provider) is False:
                return _with_provider(
                    candidate_provider,
                    "LLM key cannot be reused across image endpoint origins",
                )
    return _with_provider(
        provider,
        (
            "same provider key"
            if source == "llm_fallback"
            else _source_detail(source, env_key)
        ),
    )


def _memory_embedding_annotations(
    cfg: GatewayConfig,
    status: SectionStatus,
) -> tuple[str, str]:
    memory = getattr(cfg, "memory", None)
    embedding = getattr(memory, "embedding", None)
    if embedding is None:
        return "none", ""
    provider = str(getattr(embedding, "requested_provider", "") or "auto")
    if provider in {"none", "auto", "local", "ollama"}:
        return "not_required", ""
    remote = getattr(embedding, "remote", None)
    key = (
        str(getattr(remote, "api_key", "") or "")
        or str(getattr(embedding, "api_key", "") or "")
    )
    if key:
        return "explicit", ""
    env_key = str(getattr(remote, "api_key_env", "") or "").strip()
    if env_key and os.environ.get(env_key):
        return "env", env_key
    if env_key:
        return "missing_env", env_key
    if status is SectionStatus.OK:
        return "env", env_key
    return "none", env_key


def _audio_annotations(
    cfg: GatewayConfig,
    status: SectionStatus,
) -> tuple[str, str, str]:
    audio_cfg = getattr(cfg, "audio", None)
    if audio_cfg is None or status is SectionStatus.OPTIONAL:
        return "none", "", ""
    provider_id = "elevenlabs"
    try:
        spec = get_audio_provider_setup_spec(provider_id)
    except KeyError:
        return "none", provider_id, ""
    providers = getattr(audio_cfg, "providers", None)
    provider_cfg = getattr(providers, provider_id, None) if providers is not None else None
    if provider_cfg is None:
        return "none", provider_id, spec.env_key
    if getattr(provider_cfg, "api_key", ""):
        return "explicit", provider_id, ""
    env_key = str(getattr(provider_cfg, "api_key_env", "") or spec.env_key).strip()
    if env_key and os.environ.get(env_key):
        return "env", provider_id, env_key
    if env_key:
        return "missing_env", provider_id, env_key
    return "none", provider_id, spec.env_key


def _runtime_blocking_sections(
    *,
    memory_provider: str,
    memory_status: SectionStatus,
) -> set[str]:
    blocking: set[str] = set()
    if (
        memory_provider in {"openai", "openai-compatible"}
        and memory_status not in (SectionStatus.OK, SectionStatus.OPTIONAL)
    ):
        blocking.add("memory_embedding")
    return blocking


def get_onboarding_status(
    config: GatewayConfig,
    *,
    probe_history: dict[str, dict[str, object]] | None = None,
) -> OnboardingStatus:
    path = Path(config.config_path).expanduser() if config.config_path else default_config_path()
    has_config = path.exists()

    sections = {name: verifier(config) for name, verifier in section_verifiers().items()}

    llm_status = sections["llm"]
    search_status = sections["search"]
    image_status = sections["image_generation"]
    audio_status = sections["audio"]
    memory_status = sections["memory_embedding"]
    llm_source, llm_env_key = _llm_source(config, llm_status)
    search_provider, search_source, search_env_key = _search_annotations(
        config, search_status
    )
    image_source, image_provider, image_primary, image_env_key = _image_generation_annotations(
        config, image_status
    )
    audio_source, audio_provider, audio_env_key = _audio_annotations(config, audio_status)
    memory_embedding = getattr(getattr(config, "memory", None), "embedding", None)
    memory_provider = str(
        getattr(memory_embedding, "requested_provider", "")
        or getattr(memory_embedding, "provider", "")
        or ""
    )
    memory_source, memory_env_key = _memory_embedding_annotations(config, memory_status)
    runtime_blocking = _runtime_blocking_sections(
        memory_provider=memory_provider,
        memory_status=memory_status,
    )
    detail_text = {
        "llm": _source_detail(llm_source, llm_env_key),
        "router": _router_detail(config, llm_source),
        "ensemble": _ensemble_detail(config),
        "search": _source_detail(search_source, search_env_key),
        "image_generation": _image_generation_detail(
            config,
            image_status,
            image_source,
            image_provider,
            image_env_key,
        ),
        "audio": _with_provider(
            audio_provider,
            _source_detail(audio_source, audio_env_key),
        ),
        "memory_embedding": _with_provider(
            memory_provider,
            _source_detail(memory_source, memory_env_key),
        ),
    }

    enabled_channels = [c for c in config.channels.channels if c.enabled]

    section_details = _section_details(sections, detail_text, runtime_blocking)
    if "router" in section_details:
        # Additive read-side key on the router card only: an explicit
        # server-computed mode so clients stop inferring it from
        # (provider, tier_profile) pairs. Contract-frozen in
        # tests/test_contracts/test_onboarding_status.py.
        section_details["router"]["routerMode"] = _router_mode(config)
        section_details["router"]["routerBinding"] = _router_binding(config)
        section_details["router"]["routerProviderConflicts"] = list(
            _router_provider_conflicts(config)
        )
        section_details["router"]["routerProviderRoles"] = router_tier_provider_roles(
            getattr(config.squilla_router, "tiers", {}) or {},
            shared_selection_mode=effective_ensemble_selection_mode(config),
            ensemble_globally_enabled=bool(
                getattr(getattr(config, "llm_ensemble", None), "enabled", False)
            ),
        )
        tier_ensemble_statuses = _tier_ensemble_onboarding_statuses(config)
        section_details["router"]["tierEnsembleStatuses"] = tier_ensemble_statuses
        # Compatibility field for the current C3 editor.  Its meaning is now
        # explicit: never infer C3 readiness from another tier's legacy mode.
        section_details["router"]["tierEnsembleStatus"] = (
            tier_ensemble_statuses.get("c3")
        )
    if "ensemble" in section_details:
        section_details["ensemble"].update(_ensemble_onboarding_status(config))
    resolution_getter = getattr(config, "provider_resolution", None)
    provider_resolution = (
        resolution_getter() if callable(resolution_getter) else {}
    )
    if "llm" in section_details and provider_resolution:
        effective_provider = provider_resolution.get("effective_provider")
        section_details["llm"]["providerResolution"] = {
            "status": str(provider_resolution.get("status") or "explicit"),
            "effectiveProvider": str(
                getattr(config.llm, "provider", "")
                if effective_provider is None
                else effective_provider
            ),
            "source": str(provider_resolution.get("source") or "config"),
            "reasonCode": str(
                provider_resolution.get("reason_code") or "provider_explicit"
            ),
            "actionRequired": bool(
                provider_resolution.get("action_required", False)
            ),
            "actionRecommended": bool(
                provider_resolution.get("action_recommended", False)
            ),
        }

    warnings: tuple[str, ...] = ()
    if bool(provider_resolution.get("action_required", False)):
        warnings = (
            "Provider evidence conflicts; choose and save the intended provider "
            "before sending model requests.",
        )
    elif bool(provider_resolution.get("action_recommended", False)):
        warnings = (
            "Provider was inferred for compatibility; save the intended provider "
            "to make the identity explicit.",
        )

    llm_profile_status = _llm_profile_status(config, probe_history=probe_history)
    image_generation_state = resolve_image_generation_state(
        config,
        configured=image_status is SectionStatus.OK,
        resolved_provider_id=image_provider,
        credential_source=image_source,
        section_status=image_status.value,
    )
    return OnboardingStatus(
        config_path=str(path),
        has_config=has_config,
        llm_configured=llm_status is SectionStatus.OK,
        llm_source=llm_source,
        llm_env_key=llm_env_key,
        search_configured=search_status is SectionStatus.OK,
        search_provider=search_provider,
        search_source=search_source,
        search_env_key=search_env_key,
        image_generation_configured=image_status is SectionStatus.OK,
        image_generation_enabled=bool(getattr(config.image_generation, "enabled", False)),
        image_generation_source=image_source,
        image_generation_provider=image_provider,
        image_generation_primary=image_primary,
        image_generation_env_key=image_env_key,
        image_generation_state=image_generation_state,
        audio_configured=audio_status is SectionStatus.OK,
        audio_enabled=bool(getattr(config.audio, "enabled", False)),
        audio_source=audio_source,
        audio_provider=audio_provider,
        audio_env_key=audio_env_key,
        memory_embedding_configured=memory_status is SectionStatus.OK,
        memory_embedding_provider=memory_provider,
        memory_embedding_source=memory_source,
        memory_embedding_env_key=memory_env_key,
        channel_count=len(config.channels.channels),
        channels_configured=bool(enabled_channels),
        needs_onboarding=_needs_onboarding(sections) or bool(runtime_blocking),
        llm_credential_status=_llm_credential_status(config),
        llm_profile_status=llm_profile_status,
        ensemble_credential_status=_ensemble_credential_status(
            config,
            llm_profile_status,
        ),
        sections=sections,
        section_details=section_details,
        warnings=warnings,
    )


__all__ = [
    "OnboardingStatus",
    "SectionStatus",
    "get_onboarding_status",
    "channels_section_status",
    "audio_section_status",
    "ensemble_section_status",
    "image_generation_section_status",
    "llm_section_status",
    "memory_embedding_section_status",
    "router_section_status",
    "search_section_status",
]
