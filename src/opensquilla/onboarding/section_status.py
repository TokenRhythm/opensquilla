"""Declarative per-section verifiers for onboarding readiness.

Each verifier is a pure function ``(cfg) -> SectionStatus`` that reflects the
current state of one onboarding section as derived from the gateway config.
Verifiers never raise — internal lookup failures map to ``UNKNOWN`` so
``get_onboarding_status`` and ``--if-needed`` stay total functions over
arbitrary configs.

This module is the single source of truth consulted by:

* ``onboard --if-needed`` to decide whether onboarding can be skipped
* ``opensquilla onboard status`` to render an at-a-glance readiness table
* ``OnboardingStatus`` (status.py) to recompute the legacy boolean fields
  while keeping the existing WebUI / RPC contract intact

Adding a new section means writing one verifier here and registering it in
``section_verifiers()``; no other call site needs to change.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Collection
from enum import StrEnum
from typing import Any

from opensquilla.endpoint_identity import credential_env_for_endpoint
from opensquilla.gateway.config import GatewayConfig, ImageGenerationConfig
from opensquilla.onboarding.audio_specs import get_audio_provider_setup_spec
from opensquilla.onboarding.image_generation_specs import (
    get_image_generation_provider_setup_spec,
    list_image_generation_provider_setup_specs,
)
from opensquilla.onboarding.provider_specs import get_provider_setup_spec
from opensquilla.onboarding.search_specs import get_search_provider_setup_spec
from opensquilla.provider.image_generation_credentials import (
    resolve_image_generation_credential,
)
from opensquilla.provider.image_generation_policy import (
    conflicting_image_generation_endpoint_provider,
    is_valid_image_generation_base_url,
    parse_image_generation_model_ref,
    resolve_image_generation_base_url,
)

FIRST_RUN_REQUIRED_SECTIONS = frozenset({"llm"})

# The packaged default image-generation primary model, read straight from the
# pydantic field default so onboarding provider resolution can never drift
# from what ``GatewayConfig`` actually ships.
DEFAULT_IMAGE_GENERATION_PRIMARY: str = str(
    ImageGenerationConfig.model_fields["primary"].default
)


class SectionStatus(StrEnum):
    """Readiness state of one onboarding section.

    The naming is user-facing: ``MISSING`` for unfinished setup,
    ``DEGRADED`` for "user told us to use an env var that isn't set right now",
    ``OPTIONAL`` for sections the user intentionally opted out of,
    ``UNKNOWN`` for verifier-side lookup failures. ``StrEnum`` keeps the values
    JSON-serialisable for the ``onboard status --json`` output.
    """

    OK = "ok"
    MISSING = "missing"
    DEGRADED = "degraded"
    OPTIONAL = "optional"
    UNKNOWN = "unknown"


# Single source of truth for the operator-facing status words. Every CLI
# renderer (the ``onboard status`` table and the next-steps capability
# summary) must display a section state through this mapping so the wording
# can never drift between surfaces.
SECTION_STATUS_DISPLAY: dict[SectionStatus, str] = {
    SectionStatus.OK: "Ready",
    SectionStatus.OPTIONAL: "Later",
    SectionStatus.MISSING: "Missing",
    SectionStatus.DEGRADED: "Needs action",
    SectionStatus.UNKNOWN: "Check",
}


def _str(cfg: object, name: str) -> str:
    return (getattr(cfg, name, "") or "").strip()


def llm_section_status(cfg: GatewayConfig) -> SectionStatus:
    """LLM is the only section that never legitimately resolves to OPTIONAL.

    The runtime cannot operate without a usable language-model provider, so a
    missing or undecidable LLM always blocks onboarding.
    """
    llm = cfg.llm
    resolution_getter = getattr(cfg, "provider_resolution", None)
    resolution = resolution_getter() if callable(resolution_getter) else {}
    if bool(resolution.get("action_required", False)):
        return SectionStatus.DEGRADED
    if not _str(llm, "provider") or not _str(llm, "model"):
        return SectionStatus.MISSING
    try:
        spec = get_provider_setup_spec(llm.provider)
    except KeyError:
        return SectionStatus.UNKNOWN
    if not spec.runtime_supported:
        return SectionStatus.UNKNOWN
    if spec.requires_base_url and not _str(llm, "base_url"):
        return SectionStatus.MISSING
    if not spec.requires_api_key:
        return SectionStatus.OK
    if llm.api_key and "llm.api_key" not in getattr(cfg, "_runtime_secret_paths", set()):
        return SectionStatus.OK
    env_key = _str(llm, "api_key_env")
    if env_key:
        return SectionStatus.OK if os.environ.get(env_key) else SectionStatus.DEGRADED
    # Fall back to the provider's default env var (e.g. OPENROUTER_API_KEY) just
    # like the runtime (resolve_llm_runtime_config) and the image-generation
    # credential check: a resolvable default env var counts as configured.
    if spec.env_key and os.environ.get(spec.env_key):
        return SectionStatus.OK
    return SectionStatus.MISSING


def router_section_status(cfg: GatewayConfig) -> SectionStatus:
    """``enabled=False`` is a deliberate operator choice, not a problem.

    ``SquillaRouterConfig`` does not carry a ``mode`` field — ``upsert_router``
    flips ``enabled`` and ``tier_profile`` according to the onboard option.
    A disabled router is the canonical "I do not want local routing" state.
    """
    router = getattr(cfg, "squilla_router", None)
    if router is None:
        return SectionStatus.OPTIONAL
    return SectionStatus.OK if bool(getattr(router, "enabled", False)) else SectionStatus.OPTIONAL


def ensemble_section_status(cfg: GatewayConfig) -> SectionStatus:
    """``[llm_ensemble]`` mirrors the router: disabled is an opt-out, not a gap.

    The ensemble reuses the active provider credential, so there is no
    section-local key to verify — the section is ``OK`` when enabled and
    ``OPTIONAL`` when disabled, and it never blocks onboarding.
    """
    ensemble = getattr(cfg, "llm_ensemble", None)
    if ensemble is None:
        return SectionStatus.OPTIONAL
    return (
        SectionStatus.OK
        if bool(getattr(ensemble, "enabled", False))
        else SectionStatus.OPTIONAL
    )


def search_section_status(cfg: GatewayConfig) -> SectionStatus:
    provider = _str(cfg, "search_provider")
    if not provider:
        return SectionStatus.OPTIONAL
    try:
        spec = get_search_provider_setup_spec(provider)
    except KeyError:
        return SectionStatus.UNKNOWN
    if not spec.requires_api_key:
        return SectionStatus.OK
    if getattr(cfg, "search_api_key", ""):
        return SectionStatus.OK
    env_key = _str(cfg, "search_api_key_env")
    if env_key:
        return SectionStatus.OK if os.environ.get(env_key) else SectionStatus.DEGRADED
    # Mirror the LLM/image-gen credential checks: the provider's default env var
    # (e.g. BRAVE_SEARCH_API_KEY) resolving in the environment counts as configured.
    if spec.env_key and os.environ.get(spec.env_key):
        return SectionStatus.OK
    return SectionStatus.MISSING


def channels_section_status(cfg: GatewayConfig) -> SectionStatus:
    """Empty or all-disabled channel list reads as an opt-out, not a failure."""
    channels = list(getattr(cfg.channels, "channels", []) or [])
    if any(getattr(c, "enabled", False) for c in channels):
        return SectionStatus.OK
    return SectionStatus.OPTIONAL


def image_generation_section_status(cfg: GatewayConfig) -> SectionStatus:
    image_cfg = getattr(cfg, "image_generation", None)
    if image_cfg is None or not bool(getattr(image_cfg, "enabled", False)):
        return SectionStatus.OPTIONAL
    if _image_generation_has_invalid_model_reference(cfg):
        return SectionStatus.UNKNOWN
    provider_ids = _configured_image_generation_provider_ids(cfg)
    if str(getattr(image_cfg, "binding", "custom") or "custom") == "follow_llm":
        route_provider = provider_ids[0] if provider_ids else ""
        try:
            route_spec = get_image_generation_provider_setup_spec(route_provider)
        except KeyError:
            return SectionStatus.UNKNOWN
        endpoint = _image_generation_effective_endpoint(cfg, route_provider)
        provider_cfg = _image_generation_provider_config(cfg, route_provider)
        if endpoint is None:
            return SectionStatus.UNKNOWN
        effective_endpoint = route_spec.default_base_url
        route_credential = resolve_image_generation_credential(
            provider_id=route_provider,
            provider_config=provider_cfg,
            default_env_key=route_spec.env_key,
            default_base_url=endpoint[0],
            effective_base_url=effective_endpoint,
            gateway_config=cfg,
            model=str(getattr(image_cfg, "primary", "") or "image-generation"),
            include_image_credentials=False,
        )
        if not (
            route_credential.available
            and route_credential.owner in {"primary", "profile"}
        ):
            if (
                route_credential.owner in {"primary", "profile"}
                and route_credential.source == "missing_env"
            ):
                return SectionStatus.DEGRADED
            return SectionStatus.OPTIONAL
    # A fallback can keep generation available after a local primary failure,
    # but it must not conceal a persisted provider/official-endpoint mismatch.
    # The operator still needs to repair the primary routing before the section
    # can honestly report a healthy configuration.
    if any(
        _image_generation_endpoint_conflict_provider(cfg, provider_id) is not None
        for provider_id in provider_ids
    ):
        return SectionStatus.DEGRADED
    if any(
        _image_generation_endpoint_is_valid(cfg, provider_id) is False
        for provider_id in provider_ids
    ):
        return SectionStatus.DEGRADED
    aggregate = SectionStatus.MISSING
    for provider_id in provider_ids:
        credential = _image_generation_credential_state(cfg, provider_id)
        if credential is SectionStatus.OK:
            return SectionStatus.OK
        # ``UNKNOWN`` from a bad provider reference should win over a plain
        # ``MISSING`` from a credential-less but valid provider so the
        # operator sees the config-shape problem first; ``DEGRADED`` still
        # beats ``MISSING`` for the same reason as LLM/search.
        if credential is SectionStatus.UNKNOWN:
            aggregate = SectionStatus.UNKNOWN
        elif credential is SectionStatus.DEGRADED and aggregate is not SectionStatus.UNKNOWN:
            aggregate = SectionStatus.DEGRADED
    return aggregate


def audio_section_status(cfg: GatewayConfig) -> SectionStatus:
    audio_cfg = getattr(cfg, "audio", None)
    if audio_cfg is None or not bool(getattr(audio_cfg, "enabled", False)):
        return SectionStatus.OPTIONAL
    provider_id = "elevenlabs"
    try:
        _spec = get_audio_provider_setup_spec(provider_id)
    except KeyError:
        return SectionStatus.UNKNOWN
    providers = getattr(audio_cfg, "providers", None)
    provider_cfg = getattr(providers, provider_id, None) if providers is not None else None
    if provider_cfg is None:
        return SectionStatus.UNKNOWN
    if getattr(provider_cfg, "api_key", ""):
        return SectionStatus.OK
    env_key = str(getattr(provider_cfg, "api_key_env", "") or "").strip()
    if env_key:
        return SectionStatus.OK if os.environ.get(env_key) else SectionStatus.DEGRADED
    return SectionStatus.MISSING


def memory_embedding_section_status(cfg: GatewayConfig) -> SectionStatus:
    """Memory embedding is optional unless the operator selected remote mode.

    The default ``auto`` path is considered locally usable because it falls
    back to the bundled/on-device embedding stack. ``none`` is an explicit
    opt-out. Remote embedding providers can use either a stored key or an
    env-key reference. A configured but currently missing env var is degraded
    rather than missing, matching the other onboarding key surfaces.
    """
    memory = getattr(cfg, "memory", None)
    embedding = getattr(memory, "embedding", None)
    if embedding is None:
        return SectionStatus.OPTIONAL
    provider = str(getattr(embedding, "requested_provider", "") or "auto")
    if provider == "none":
        return SectionStatus.OPTIONAL
    if provider in {"auto", "local", "ollama"}:
        return SectionStatus.OK
    if provider in {"openai", "openai-compatible"}:
        remote = getattr(embedding, "remote", None)
        key = (
            str(getattr(remote, "api_key", "") or "")
            or str(getattr(embedding, "api_key", "") or "")
        )
        if key:
            return SectionStatus.OK
        env_key = str(getattr(remote, "api_key_env", "") or "").strip()
        if env_key:
            return SectionStatus.OK if os.environ.get(env_key) else SectionStatus.DEGRADED
        return SectionStatus.MISSING
    return SectionStatus.UNKNOWN


def _image_generation_credential_state(
    cfg: GatewayConfig,
    provider_id: str,
) -> SectionStatus:
    """Mirror ``llm`` / ``search`` credential semantics for image generation.

    Returns one of ``OK / MISSING / DEGRADED / UNKNOWN`` so the section-level
    reducer can preserve the contract of the broader ``SectionStatus`` enum.

    Resolution order (each branch wins if it produces ``OK``):
      1. explicit ``provider_cfg.api_key`` (paste) -> ``OK``
      2. operator-explicit env_key resolved in ``os.environ`` -> ``OK``
      3. operator-explicit env_key declared but absent -> ``DEGRADED``
      4. spec default env_key resolved in ``os.environ`` -> ``OK``
      5. matching LLM provider with an explicit ``api_key`` or resolved
         ``api_key_env`` on the same endpoint -> ``OK``
      6. matching LLM provider with a missing env reference or credential on
         another endpoint -> ``DEGRADED``
      7. otherwise -> ``MISSING``

    The configured env provenance mirrors the runtime resolver: the registry
    default env var follows only the registry endpoint, while any operator
    authored reference (including an explicit default-name reference) may be
    used for a custom compatible endpoint.
    """
    try:
        spec = get_image_generation_provider_setup_spec(provider_id)
    except KeyError:
        return SectionStatus.UNKNOWN

    providers = getattr(getattr(cfg, "image_generation", None), "providers", None)
    provider_cfg = getattr(providers, provider_id, None) if providers is not None else None
    endpoint = _image_generation_effective_endpoint(cfg, provider_id)
    if endpoint is None:
        return SectionStatus.UNKNOWN
    default_base_url, effective_base_url = endpoint
    resolution = resolve_image_generation_credential(
        provider_id=provider_id,
        provider_config=provider_cfg,
        default_env_key=spec.env_key,
        default_base_url=default_base_url,
        effective_base_url=effective_base_url,
        gateway_config=cfg,
        model=str(getattr(cfg.image_generation, "primary", "") or "image-generation"),
    )
    if resolution.available:
        return SectionStatus.OK
    if resolution.source == "missing_env" or resolution.reason == "endpoint_mismatch":
        return SectionStatus.DEGRADED
    return SectionStatus.MISSING


def _image_generation_provider_config(
    cfg: GatewayConfig,
    provider_id: str,
) -> object | None:
    providers = getattr(getattr(cfg, "image_generation", None), "providers", None)
    return getattr(providers, provider_id, None) if providers is not None else None


def _image_generation_effective_env_key(
    cfg: GatewayConfig,
    provider_id: str,
    spec: Any,
) -> tuple[str, bool]:
    """Resolve the env source using the same endpoint provenance as runtime."""

    providers = getattr(getattr(cfg, "image_generation", None), "providers", None)
    provider_cfg = getattr(providers, provider_id, None) if providers is not None else None
    cfg_env_key = (
        str(getattr(provider_cfg, "api_key_env", "") or "").strip()
        if provider_cfg is not None
        else ""
    )
    fields_set = getattr(provider_cfg, "model_fields_set", None)
    env_was_explicitly_configured = (
        isinstance(fields_set, set) and "api_key_env" in fields_set
    )
    endpoint = _image_generation_effective_endpoint(cfg, provider_id)
    if endpoint is None:
        return "", False
    default_base_url, effective_base_url = endpoint
    spec_env_key = str(getattr(spec, "env_key", "") or "").strip()
    resolved_env_key = credential_env_for_endpoint(
        configured_env=cfg_env_key,
        configured_explicitly=env_was_explicitly_configured,
        default_env=spec_env_key,
        default_base_url=default_base_url,
        effective_base_url=effective_base_url,
    )
    env_is_explicit = bool(
        resolved_env_key
        and cfg_env_key
        and (cfg_env_key != spec_env_key or env_was_explicitly_configured)
    )
    return resolved_env_key, env_is_explicit


def _image_generation_endpoint_conflict_provider(
    cfg: GatewayConfig,
    provider_id: str,
) -> str | None:
    """Return the official provider that conflicts with this image endpoint."""

    endpoint = _image_generation_effective_endpoint(cfg, provider_id)
    if endpoint is None:
        return None
    _default_base_url, base_url = endpoint
    return conflicting_image_generation_endpoint_provider(
        provider_id,
        base_url,
    )


def _image_generation_endpoint_is_valid(
    cfg: GatewayConfig,
    provider_id: str,
) -> bool | None:
    endpoint = _image_generation_effective_endpoint(cfg, provider_id)
    if endpoint is None:
        return None
    return is_valid_image_generation_base_url(endpoint[1])


def _image_generation_llm_key_reusable(
    cfg: GatewayConfig,
    provider_id: str,
) -> bool | None:
    endpoint = _image_generation_effective_endpoint(cfg, provider_id)
    if endpoint is None:
        return None
    default_base_url, effective_base_url = endpoint
    try:
        spec = get_image_generation_provider_setup_spec(provider_id)
    except KeyError:
        return None
    providers = getattr(getattr(cfg, "image_generation", None), "providers", None)
    provider_cfg = getattr(providers, provider_id, None) if providers is not None else None
    resolution = resolve_image_generation_credential(
        provider_id=provider_id,
        provider_config=provider_cfg,
        default_env_key=spec.env_key,
        default_base_url=default_base_url,
        effective_base_url=effective_base_url,
        gateway_config=cfg,
    )
    if resolution.source == "llm_fallback":
        return True
    if resolution.owner in {"primary", "profile"} and resolution.reason == "endpoint_mismatch":
        return False
    return None


def _image_generation_llm_env_key(
    cfg: GatewayConfig,
    provider_id: str,
) -> str:
    """Return the active same-provider LLM env reference, if one is authored."""

    endpoint = _image_generation_effective_endpoint(cfg, provider_id)
    if endpoint is None:
        return ""
    try:
        spec = get_image_generation_provider_setup_spec(provider_id)
    except KeyError:
        return ""
    providers = getattr(getattr(cfg, "image_generation", None), "providers", None)
    provider_cfg = getattr(providers, provider_id, None) if providers is not None else None
    resolution = resolve_image_generation_credential(
        provider_id=provider_id,
        provider_config=provider_cfg,
        default_env_key=spec.env_key,
        default_base_url=endpoint[0],
        effective_base_url=endpoint[1],
        gateway_config=cfg,
    )
    return resolution.env_key if resolution.owner in {"primary", "profile"} else ""


def _image_generation_effective_endpoint(
    cfg: GatewayConfig,
    provider_id: str,
) -> tuple[str, str] | None:
    try:
        spec = get_image_generation_provider_setup_spec(provider_id)
    except KeyError:
        return None
    providers = getattr(getattr(cfg, "image_generation", None), "providers", None)
    provider_cfg = getattr(providers, provider_id, None) if providers is not None else None
    return (
        spec.default_base_url,
        resolve_image_generation_base_url(
            provider_id=provider_id,
            provider_config=provider_cfg,
            llm_config=getattr(cfg, "llm", None),
            default_base_url=spec.default_base_url,
            gateway_config=cfg,
        ),
    )


def _image_generation_provider_has_operator_credential(
    cfg: GatewayConfig,
    provider_id: str,
    spec: Any,
) -> bool:
    providers = getattr(getattr(cfg, "image_generation", None), "providers", None)
    provider_cfg = getattr(providers, provider_id, None) if providers is not None else None
    if provider_cfg is None:
        return False
    if getattr(provider_cfg, "api_key", ""):
        return True
    spec_env_key = (getattr(spec, "env_key", "") or "").strip()
    cfg_env_key = (getattr(provider_cfg, "api_key_env", "") or "").strip()
    return bool(cfg_env_key and cfg_env_key != spec_env_key)


def section_verifiers() -> dict[str, Callable[[GatewayConfig], SectionStatus]]:
    """Registry consumed by ``get_onboarding_status`` and ``onboard status``."""
    return {
        "llm": llm_section_status,
        "router": router_section_status,
        "ensemble": ensemble_section_status,
        "search": search_section_status,
        "channels": channels_section_status,
        "image_generation": image_generation_section_status,
        "audio": audio_section_status,
        "memory_embedding": memory_embedding_section_status,
    }


def needs_onboarding(
    sections: dict[str, SectionStatus],
    *,
    required_sections: Collection[str] = FIRST_RUN_REQUIRED_SECTIONS,
) -> bool:
    """Required non-OK, non-OPTIONAL sections mean onboarding is still blocking.

    Optional sections still surface their own action-required status through
    ``OnboardingStatus.section_details`` but do not keep ``--if-needed`` in the
    first-run wizard.
    """
    return any(
        sections.get(name, SectionStatus.UNKNOWN)
        not in (SectionStatus.OK, SectionStatus.OPTIONAL)
        for name in required_sections
    )


def _configured_image_generation_provider_ids(cfg: GatewayConfig) -> list[str]:
    """Resolve which image-generation providers the config points at.

    Shared by the section verifier above and by ``status.py``'s annotation
    derivation — keep this the only implementation of the resolution order
    (operator credentials beat model routing beats spec defaults).
    """
    image_cfg = cfg.image_generation
    primary = getattr(image_cfg, "primary", "")
    fallbacks = list(getattr(image_cfg, "fallbacks", []) or [])
    explicit_routing = bool(fallbacks) or bool(
        primary and primary != DEFAULT_IMAGE_GENERATION_PRIMARY
    )
    specs = [
        spec
        for spec in list_image_generation_provider_setup_specs()
        if spec.runtime_supported
    ]
    explicit_provider_ids = [
        spec.provider_id
        for spec in specs
        if _image_generation_provider_has_operator_credential(
            cfg,
            spec.provider_id,
            spec,
        )
    ]
    if not explicit_routing and explicit_provider_ids:
        return explicit_provider_ids
    refs = (
        [primary, *fallbacks]
        if explicit_routing
        else [spec.default_model for spec in specs]
    )
    seen: set[str] = set()
    result: list[str] = []
    for ref in refs:
        try:
            provider_id, _model = parse_image_generation_model_ref(ref)
        except ValueError:
            continue
        if provider_id not in seen:
            seen.add(provider_id)
            result.append(provider_id)
    return result


def _image_generation_has_invalid_model_reference(cfg: GatewayConfig) -> bool:
    image_cfg = cfg.image_generation
    primary = str(getattr(image_cfg, "primary", "") or "")
    fallbacks = list(getattr(image_cfg, "fallbacks", []) or [])
    explicit_routing = bool(fallbacks) or bool(
        primary and primary != DEFAULT_IMAGE_GENERATION_PRIMARY
    )
    if not explicit_routing:
        return False
    for ref in [primary, *fallbacks]:
        try:
            parse_image_generation_model_ref(ref)
        except ValueError:
            return True
    return False
