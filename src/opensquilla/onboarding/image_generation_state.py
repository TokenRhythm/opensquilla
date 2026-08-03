"""Image-generation ownership, binding, and recommendation contracts.

This module deliberately contains no runtime registry construction.  It is
the shared policy boundary used by onboarding mutations and status payloads;
the media runtime consumes the persisted ``binding`` and effective provider
separately.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

from opensquilla.endpoint_identity import base_url_matches_official_api
from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.image_generation_specs import (
    list_image_generation_provider_setup_specs,
)
from opensquilla.onboarding.provider_specs import get_provider_setup_spec
from opensquilla.provider.image_generation_credentials import (
    resolve_image_generation_credential,
)
from opensquilla.provider.image_generation_policy import (
    parse_image_generation_model_ref,
    resolve_image_generation_base_url,
)

ImageGenerationIntent = Literal["preserve", "enable_provider_default"]

OPENROUTER_IMAGE_PRIMARY = "openrouter/google/gemini-3.1-flash-image-preview"
_IMAGE_GENERATION_INTENTS = frozenset({"preserve", "enable_provider_default"})


def default_image_generation_intent_for_provider(
    provider_id: str,
) -> ImageGenerationIntent:
    """Return the first-party setup default for an LLM provider."""

    return (
        "enable_provider_default"
        if str(provider_id or "").strip().lower() == "openrouter"
        else "preserve"
    )


def normalize_image_generation_intent(value: str | None) -> ImageGenerationIntent:
    """Validate the additive LLM-save intent without changing legacy defaults."""

    normalized = "preserve" if value is None else str(value).strip().lower()
    if normalized not in _IMAGE_GENERATION_INTENTS:
        expected = ", ".join(sorted(_IMAGE_GENERATION_INTENTS))
        raise ValueError(f"image_generation_intent must be one of: {expected}")
    return cast(ImageGenerationIntent, normalized)


def image_generation_is_operator_managed(config: GatewayConfig) -> bool:
    """Return whether an image section or environment override is authored.

    Loaded configurations carry their raw TOML snapshot, which lets us tell
    an absent section from schema defaults.  Directly-constructed configs
    (tests and embedding integrations) fall back to Pydantic field provenance.
    An explicit empty section and ``enabled = false`` both count as ownership:
    neither may be overwritten by an LLM save. The sole exception is an
    enabled ``follow_llm`` section, which is the persisted system default.
    """

    image_config = getattr(config, "image_generation", None)
    binding = str(getattr(image_config, "binding", "custom") or "custom")
    enabled = bool(getattr(image_config, "enabled", False))
    # ``follow_llm`` is the one system-owned persisted shape. An explicit off
    # remains operator-owned even if it came from an early build that retained
    # the old binding value.
    if binding == "follow_llm" and enabled:
        return False

    raw = getattr(config, "_persist_raw_base", None)
    if isinstance(raw, Mapping) and "image_generation" in raw:
        return True

    image_fields: object = getattr(image_config, "model_fields_set", set())
    if isinstance(image_fields, set) and image_fields:
        return True

    # A loaded config with an authoritative baseline and no raw image section
    # is unowned.  The field-provenance check above still protects settings
    # supplied through OPENSQUILLA_IMAGE_GENERATION_*.
    if getattr(config, "_persist_baseline", None) is not None:
        return False
    return "image_generation" in getattr(config, "model_fields_set", set())


def _official_active_provider(config: GatewayConfig, provider_id: str) -> bool:
    provider = str(provider_id or "").strip().lower()
    llm = getattr(config, "llm", None)
    if str(getattr(llm, "provider", "") or "").strip().lower() != provider:
        return False
    try:
        default_base_url = get_provider_setup_spec(provider).default_base_url
    except KeyError:
        return False
    effective_base_url = str(getattr(llm, "base_url", "") or default_base_url).strip()
    return bool(default_base_url) and base_url_matches_official_api(
        default_base_url,
        effective_base_url,
    )


def apply_image_generation_intent(
    source: GatewayConfig,
    target: GatewayConfig,
    *,
    provider_id: str,
    intent: str | None,
) -> dict[str, object] | None:
    """Apply an explicit OpenRouter default in the surrounding LLM mutation.

    ``source`` is inspected for ownership while ``target`` contains the newly
    selected active LLM.  Keeping both in one pure mutation gives the RPC
    caller a single persistence boundary, so LLM and image defaults cannot be
    half-applied.
    """

    normalized = normalize_image_generation_intent(intent)
    if normalized == "preserve":
        return None

    provider = str(provider_id or "").strip().lower()
    reason = "enabled_provider_default"
    applied = False
    if provider != "openrouter":
        reason = "provider_not_supported"
    elif image_generation_is_operator_managed(source):
        reason = "operator_configuration_preserved"
    elif not _official_active_provider(target, "openrouter"):
        reason = "custom_endpoint_preserved"
    else:
        image_config = target.image_generation
        image_config.enabled = True
        image_config.binding = "follow_llm"
        image_config.primary = OPENROUTER_IMAGE_PRIMARY
        image_config.fallbacks = []
        target.mark_force_persist("image_generation.enabled")
        target.mark_force_persist("image_generation.binding")
        target.mark_force_persist("image_generation.primary")
        applied = True

    return {
        "applied": applied,
        "reason": reason,
        "binding": (
            str(getattr(target.image_generation, "binding", "custom") or "custom")
        ),
        "primary": str(getattr(target.image_generation, "primary", "") or ""),
    }


def _primary_provider(config: GatewayConfig) -> str:
    primary = str(getattr(config.image_generation, "primary", "") or "")
    try:
        provider, _model = parse_image_generation_model_ref(primary)
    except ValueError:
        return ""
    return provider.strip().lower()


def resolve_image_generation_state(
    config: GatewayConfig,
    *,
    configured: bool,
    resolved_provider_id: str,
    credential_source: str,
    section_status: str,
) -> dict[str, Any]:
    """Return the additive, server-authored state consumed by modern clients."""

    image_config = config.image_generation
    stored_enabled = bool(getattr(image_config, "enabled", False))
    binding = str(getattr(image_config, "binding", "custom") or "custom")
    managed = image_generation_is_operator_managed(config)
    active_provider = str(getattr(config.llm, "provider", "") or "").strip().lower()
    route_provider = _primary_provider(config)

    credential_options: list[dict[str, object]] = []
    option_by_provider: dict[str, dict[str, object]] = {}
    providers_config = getattr(image_config, "providers", None)
    for spec in list_image_generation_provider_setup_specs():
        if not spec.runtime_supported:
            continue
        provider_cfg = (
            getattr(providers_config, spec.provider_id, None)
            if providers_config is not None
            else None
        )
        endpoint = resolve_image_generation_base_url(
            provider_id=spec.provider_id,
            provider_config=provider_cfg,
            llm_config=config.llm,
            default_base_url=spec.default_base_url,
            gateway_config=config,
        )
        if binding == "follow_llm" and spec.provider_id == route_provider:
            # Legacy follow_llm grants only the official default route. A
            # custom Model Service endpoint requires an explicit custom Image
            # selection before its credential can be reused.
            endpoint = spec.default_base_url
        resolution = resolve_image_generation_credential(
            provider_id=spec.provider_id,
            provider_config=provider_cfg,
            default_env_key=spec.env_key,
            default_base_url=spec.default_base_url,
            effective_base_url=endpoint,
            gateway_config=config,
            model=spec.default_model,
            include_image_credentials=not (
                binding == "follow_llm" and spec.provider_id == route_provider
            ),
        )
        payload = resolution.public_payload()
        credential_options.append(payload)
        option_by_provider[spec.provider_id] = payload

    if not stored_enabled:
        mode = "disabled" if managed else "unconfigured"
    elif binding == "follow_llm":
        mode = "follow_llm"
    else:
        mode = "custom"

    route_option = option_by_provider.get(route_provider, {})
    dormant = bool(
        mode == "follow_llm"
        and not (
            route_option.get("available") is True
            and route_option.get("owner") in {"primary", "profile"}
        )
    )
    effective_enabled = stored_enabled and not dormant
    available = effective_enabled and bool(configured)
    effective_provider = route_provider
    if mode == "custom" and str(resolved_provider_id or "").strip():
        effective_provider = str(resolved_provider_id).strip().lower()

    if mode == "disabled":
        reason = "explicitly_disabled"
    elif mode == "unconfigured":
        reason = "not_configured"
    elif dormant:
        reason = "active_provider_mismatch"
    elif available:
        reason = "ready"
    else:
        reason = f"status_{section_status}"

    recommendation: dict[str, object] | None = None
    # TokenRhythm remains the picker recommendation whenever a non-OpenRouter
    # LLM is active, even if the operator already owns the image route. In
    # owned/disabled states it is presentation metadata only: ``actionRequired``
    # stays false, so clients do not show an intrusive setup card or mutate the
    # existing configuration.
    if (
        active_provider != "openrouter"
        or mode == "unconfigured"
        or dormant
        or mode == "follow_llm"
    ):
        active_official = _official_active_provider(config, active_provider)
        available_options = [
            option
            for option in credential_options
            if option.get("available") is True
            and (
                mode == "custom"
                or option.get("owner") in {"primary", "profile"}
            )
        ]
        available_ids = {
            str(option.get("providerId") or "") for option in available_options
        }
        needs_default_selection = mode in {"unconfigured", "follow_llm"} or dormant
        if not needs_default_selection:
            recommended_provider = "tokenrhythm"
        elif active_provider in available_ids:
            recommended_provider = active_provider
        elif "tokenrhythm" in available_ids:
            recommended_provider = "tokenrhythm"
        elif len(available_options) == 1:
            recommended_provider = str(available_options[0].get("providerId") or "")
        else:
            recommended_provider = (
                "openrouter"
                if active_provider == "openrouter" and active_official
                else "tokenrhythm"
            )
        recommended_option = option_by_provider.get(recommended_provider, {})
        can_reuse = bool(
            recommended_option.get("available") is True
        )
        recommendation = {
            "providerId": recommended_provider,
            "reason": (
                "active_llm_provider"
                if can_reuse and recommended_provider == active_provider
                else "recommended_standalone"
            ),
            "canReuseCredential": can_reuse,
            "actionRequired": mode == "unconfigured" or dormant,
        }

    return {
        "mode": mode,
        "operatorManaged": managed,
        "storedEnabled": stored_enabled,
        "effective": {
            "enabled": effective_enabled,
            "available": available,
            "dormant": dormant,
            "providerId": effective_provider if stored_enabled else "",
            "primary": str(getattr(image_config, "primary", "") or ""),
            "credentialSource": credential_source if effective_enabled else "none",
            "credentialOwner": (
                str(route_option.get("owner") or "none") if effective_enabled else "none"
            ),
            "reason": reason,
        },
        "recommendation": recommendation,
        "credentialOptions": credential_options,
    }


__all__ = [
    "ImageGenerationIntent",
    "OPENROUTER_IMAGE_PRIMARY",
    "apply_image_generation_intent",
    "default_image_generation_intent_for_provider",
    "image_generation_is_operator_managed",
    "normalize_image_generation_intent",
    "resolve_image_generation_state",
]
