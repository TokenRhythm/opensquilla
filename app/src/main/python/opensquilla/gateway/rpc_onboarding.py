"""RPC handlers for onboarding (catalog, status, provider/channel mutations).

Mutations are applied against the gateway's *active* in-memory config when the
RPC context provides one (``ctx.config``). The same context exposes the
running ``provider_selector``; provider mutations are mirrored into it so a
``configure`` from the WebUI takes effect on the next chat without a restart.

Channel mutations reconcile live through the boot-registered channels
reconciler when one is available; webhook-mode entries (HTTP routes bound at
boot) and reconciler-less contexts stay restart-gated.

The onboarding mutation/store modules import ``opensquilla.gateway.config`` at
module top level, which transitively re-enters ``opensquilla.gateway`` during
boot. To avoid the circular import, we import those bindings lazily inside the
handler bodies.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import structlog

from opensquilla.gateway.config_secrets import inherit_runtime_secrets
from opensquilla.gateway.model_routing import broadcast_model_routing_changed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.onboarding.redaction import is_redacted_secret_sentinel
from opensquilla.search.types import DEFAULT_SEARCH_MAX_RESULTS

if TYPE_CHECKING:
    from opensquilla.onboarding.config_store import CredentialBackupRedaction
    from opensquilla.onboarding.probe import ProviderProbeResult


@contextmanager
def _validation_error(code: str) -> Iterator[None]:
    """Translate a mutation validation error into a stable, client-localizable
    ``RpcHandlerError`` code, keeping the original English text as the message so
    the Web UI can fall back to it (and developers keep the detail).

    Catches both ``ValueError`` (bad fields) and ``KeyError`` (an unknown/
    unverified provider id), since on these onboarding config paths both are user
    validation failures, not internal faults. Other exceptions propagate
    unchanged and still collapse to the dispatcher's coarse codes — only the
    high-value onboarding validation paths are wrapped.
    """
    try:
        yield
    except (ValueError, KeyError) as exc:
        raise RpcHandlerError(code, str(exc)) from exc


@contextmanager
def _channel_error() -> Iterator[None]:
    """Channel mutations raise ``KeyError`` for an unknown name and ``ValueError``
    for bad fields; map them to distinct stable codes."""
    try:
        yield
    except KeyError as exc:
        raise RpcHandlerError("onboarding.channel.not_found", str(exc)) from exc
    except ValueError as exc:
        # A ChannelValidationError additionally carries per-field detail so the
        # Web UI can anchor errors to fields instead of parsing the message.
        details = getattr(exc, "field_errors", None)
        raise RpcHandlerError(
            "onboarding.channel.invalid",
            str(exc),
            details={"fields": details} if details else None,
        ) from exc

log = structlog.get_logger(__name__)

_d = get_dispatcher()


def _active_config(ctx: RpcContext) -> Any:
    """Return the gateway's running config when available, else load from disk."""
    if ctx.config is not None:
        return ctx.config
    from opensquilla.onboarding.config_store import load_config

    return load_config()


def _config_path_for(ctx: RpcContext, source: Any) -> str | None:
    """Resolve the persistence path that matches ``source``.

    Prefers the path stored on the running ``GatewayConfig`` so RPCs save back
    to wherever the gateway booted from (e.g. ``./opensquilla.toml``) rather
    than the env-default user config.
    """
    path = getattr(source, "config_path", None)
    if path:
        return str(path)
    return None


def _apply_inplace(ctx: RpcContext, new_cfg: Any) -> None:
    """Mirror new config fields into ``ctx.config`` so the running gateway sees them."""
    if ctx.config is None or ctx.config is new_cfg:
        return
    for field_name in type(new_cfg).model_fields:
        setattr(ctx.config, field_name, getattr(new_cfg, field_name))
    inherit_runtime_secrets(new_cfg, ctx.config)
    # The mutation clone started from a deep copy of ctx.config's provenance
    # state and then applied the operator's clear_runtime_override /
    # mark_force_persist decisions, so it is authoritative — adopt it
    # wholesale. Without this, a runtime-override record cleared on the
    # clone never reaches the live config, and the stale live record makes a
    # later unrelated persist rewrite the field back to the value the
    # operator just replaced (env-URL / user-URL flip-flops on disk).
    if hasattr(ctx.config, "inherit_persist_provenance") and hasattr(
        new_cfg, "_runtime_field_overrides"
    ):
        ctx.config.inherit_persist_provenance(new_cfg)


def _sync_provider_selector(ctx: RpcContext, llm_cfg: Any) -> None:
    selector = getattr(ctx, "provider_selector", None)
    if selector is None or llm_cfg is None or not hasattr(selector, "sync_primary"):
        return
    config = getattr(ctx, "config", None)
    if config is not None:
        from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config

        # Resolve on a throwaway deep copy: resolve_llm_runtime_config
        # mutates config.llm in place (env application) and records override
        # provenance, but this sync only needs the resolved runtime VALUES
        # for the selector. After _apply_inplace, ctx.config.llm IS the
        # mutation result's llm submodel — resolving against the live graph
        # would clobber an explicit operator base_url/proxy with the env
        # value right before _persist writes the file, and would record the
        # override on ctx.config only, desynchronizing it from the config
        # the persist layer actually consults.
        scratch = config.model_copy(deep=True)
        runtime = resolve_llm_runtime_config(scratch)
        api_key = runtime.api_key
        base_url = runtime.base_url
        proxy = runtime.proxy
        # Preserve the one live-config side effect the old in-place resolve
        # provided: an env-resolved api_key must stay marked as a runtime
        # secret on the running config so no persist path can write it out.
        if runtime.api_key_from_env and hasattr(config, "mark_runtime_secret"):
            config.mark_runtime_secret("llm.api_key")
    else:
        api_key = llm_cfg.api_key
        base_url = llm_cfg.base_url
        proxy = getattr(llm_cfg, "proxy", "")
    from opensquilla.provider.selector import ProviderConfig

    selector.sync_primary(
        ProviderConfig(
            provider=llm_cfg.provider,
            model=llm_cfg.model,
            api_key=api_key,
            base_url=base_url,
            proxy=proxy,
            provider_routing=getattr(llm_cfg, "provider_routing", {}),
        )
    )


def _sync_image_generation(config: Any) -> None:
    from opensquilla.tools.builtin.media import configure_audio, configure_image_generation

    configure_image_generation(
        getattr(config, "image_generation", None),
        gateway_config=config,
        llm_config=getattr(config, "llm", None),
        squilla_router_config=getattr(config, "squilla_router", None),
    )
    configure_audio(getattr(config, "audio", None))


def _sync_search_provider(config: Any) -> None:
    from opensquilla.tools.builtin.web import configure_search

    configure_search(
        provider_name=config.search_provider,
        max_results=config.search_max_results,
        api_key=config.search_api_key,
        api_key_env=getattr(config, "search_api_key_env", ""),
        proxy=config.search_proxy,
        use_env_proxy=config.search_use_env_proxy,
        fallback_policy=config.search_fallback_policy,
        diagnostics=config.search_diagnostics,
    )


def _persist(
    ctx: RpcContext,
    new_cfg: Any,
    *,
    restart_required: bool,
    backup_credential_redaction: CredentialBackupRedaction | None = None,
    remove_paths: tuple[str, ...] = (),
) -> str:
    from opensquilla.onboarding.config_store import persist_config

    # Mutation results are cloned from the active config and carry their own
    # authoritative runtime-secret markers.  Do not re-inherit the live set
    # here: an explicit credential replacement deliberately clears its old
    # env-derived marker so the new value is persisted.  Copying the marker
    # back would silently omit the replacement from disk and keep exposing the
    # startup environment credential through the live settings UI.
    path = _config_path_for(ctx, new_cfg) or _config_path_for(ctx, ctx.config)
    persist = persist_config(
        new_cfg,
        path=path,
        restart_required=restart_required,
        backup_credential_redaction=backup_credential_redaction,
        remove_paths=remove_paths,
    )
    # Preserve the resolved path on the running config so subsequent saves
    # round-trip to the same file.
    if hasattr(new_cfg, "config_path") and not getattr(new_cfg, "config_path", None):
        new_cfg.config_path = str(persist.path)
    if (
        ctx.config is not None
        and hasattr(ctx.config, "config_path")
        and not getattr(ctx.config, "config_path", None)
    ):
        ctx.config.config_path = str(persist.path)
    return str(persist.path)


def _provider_backup_credential_redaction(
    provider_id: str,
) -> CredentialBackupRedaction:
    """Build a secret-safe backup scrub request for one provider."""

    from opensquilla.onboarding.config_store import CredentialBackupRedaction

    return CredentialBackupRedaction(
        provider_id=str(provider_id or "").strip().lower(),
    )


def _status_payload(ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.legacy_data import legacy_data_payload
    from opensquilla.onboarding.mutations import capability_resettable
    from opensquilla.onboarding.next_steps import env_recovery_commands
    from opensquilla.onboarding.probe_history import load_probe_history
    from opensquilla.onboarding.status import get_onboarding_status

    cfg = _active_config(ctx)
    s = get_onboarding_status(cfg, probe_history=load_probe_history(cfg))
    llm_credential_status = dict(s.llm_credential_status)
    llm_credential_status["revealAllowed"] = bool(
        ctx.principal.is_owner
        and llm_credential_status.get("available") is True
        and llm_credential_status.get("source") in {"explicit", "env"}
    )
    return {
        "configPath": _config_path_for(ctx, cfg) or s.config_path,
        "hasConfig": s.has_config,
        "llmConfigured": s.llm_configured,
        "llmSource": s.llm_source,
        "llmEnvKey": s.llm_env_key,
        "llmCredentialStatus": llm_credential_status,
        "llmProfileStatus": list(s.llm_profile_status),
        "imageGenerationConfigured": s.image_generation_configured,
        "imageGenerationEnabled": s.image_generation_enabled,
        "imageGenerationSource": s.image_generation_source,
        "imageGenerationProvider": s.image_generation_provider,
        "imageGenerationPrimary": s.image_generation_primary,
        "imageGenerationEnvKey": s.image_generation_env_key,
        "imageGenerationState": s.image_generation_state,
        "audioConfigured": s.audio_configured,
        "audioEnabled": s.audio_enabled,
        "audioSource": s.audio_source,
        "audioProvider": s.audio_provider,
        "audioEnvKey": s.audio_env_key,
        "searchConfigured": s.search_configured,
        "searchProvider": s.search_provider,
        "searchSource": s.search_source,
        "searchEnvKey": s.search_env_key,
        "memoryEmbeddingConfigured": s.memory_embedding_configured,
        "memoryEmbeddingProvider": s.memory_embedding_provider,
        "memoryEmbeddingSource": s.memory_embedding_source,
        "memoryEmbeddingEnvKey": s.memory_embedding_env_key,
        "capabilityConfiguration": {
            capability_id: {
                "resettable": capability_resettable(cfg, capability_id=capability_id)
            }
            for capability_id in (
                "search",
                "image_generation",
                "audio",
                "memory_embedding",
            )
        },
        "channelCount": s.channel_count,
        "channelsConfigured": s.channels_configured,
        "ensembleCredentialStatus": list(s.ensemble_credential_status),
        "needsOnboarding": s.needs_onboarding,
        "sections": {name: state.value for name, state in s.sections.items()},
        "sectionDetails": s.section_details,
        "envRecoveryCommands": env_recovery_commands(s),
        "warnings": list(s.warnings),
        # Frozen compatibility key. Discovery moved to the settings-only
        # migration RPC and this value remains null through this major.
        "legacyData": legacy_data_payload(),
    }


def _active_llm_credential_reveal_payload(ctx: RpcContext, provider_id: str) -> dict[str, Any]:
    from opensquilla.gateway.llm_runtime import resolve_llm_credential
    from opensquilla.onboarding.provider_specs import get_provider_setup_spec

    if not ctx.principal.is_owner:
        raise RpcHandlerError(
            "onboarding.provider.credential.not_owner",
            "Only the local gateway owner can reveal provider credentials.",
        )

    cfg = _active_config(ctx)
    llm = getattr(cfg, "llm", None)
    active_provider = str(getattr(llm, "provider", "") or "").strip().lower()
    requested_provider = str(provider_id or "").strip().lower()
    if requested_provider != active_provider:
        raise RpcHandlerError(
            "onboarding.provider.credential.inactive_provider",
            "Credential reveal only supports the active provider.",
        )

    try:
        spec = get_provider_setup_spec(active_provider)
    except KeyError as exc:
        raise RpcHandlerError(
            "onboarding.provider.credential.unsupported_provider",
            f"Unsupported active provider: {active_provider}",
        ) from exc
    credential = resolve_llm_credential(
        cfg,
        registry_env_key=str(getattr(spec, "env_key", "") or "").strip(),
        include_runtime_cache=False,
    )
    if credential.source in {"explicit", "env"} and credential.api_key:
        return {
            "ok": True,
            "provider": active_provider,
            "source": credential.source,
            "envKey": credential.env_name,
            "apiKey": credential.api_key,
        }
    raise RpcHandlerError(
        "onboarding.provider.credential.unavailable",
        "No revealable credential is available for the active provider.",
    )


def _credential_clear_effective_payload(
    config: Any,
    provider_id: str,
    *,
    active: bool,
) -> dict[str, Any]:
    """Describe post-clear credential availability without exposing a value.

    Clearing removes stored sources only. Provider registry environment
    variables are process-owned external inputs, so an exported default key
    can remain effective after the config fields are gone. Report that state
    explicitly so clients never promise that an external credential was
    deleted.
    """
    from opensquilla.onboarding.status import get_onboarding_status

    provider = str(provider_id or "").strip().lower()
    status = get_onboarding_status(config)
    if active:
        row = dict(status.llm_credential_status)
        source = str(row.get("source") or "none")
        env_key = str(row.get("envKey") or "")
        available = bool(row.get("available"))
    else:
        row = next(
            (
                dict(candidate)
                for candidate in status.llm_profile_status
                if str(candidate.get("provider") or "").strip().lower() == provider
            ),
            {},
        )
        raw_source = str(row.get("credentialSource") or "none")
        if raw_source in {
            "member_env",
            "profile_env",
            "profile_pool",
            "profile_pool_env",
            "registry_env",
        }:
            source = "env"
        elif raw_source == "keyless":
            source = "not_required"
        elif raw_source in {"member", "profile", "inherited"}:
            source = "explicit"
        else:
            source = "none"
        env_key = str(row.get("credentialEnv") or "")
        available = source in {"explicit", "env", "not_required"}
    return {
        "credentialAvailable": available,
        "credentialSource": source,
        "credentialEnv": env_key,
        "externalCredentialActive": source == "env",
    }


@_d.method("onboarding.status", scope="operator.read")
async def _onboarding_status(params: Any, ctx: RpcContext) -> dict[str, Any]:
    return _status_payload(ctx)


@_d.method("onboarding.catalog", scope="operator.read")
async def _onboarding_catalog(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.audio_specs import audio_provider_catalog_payload
    from opensquilla.onboarding.channel_specs import channel_catalog_payload
    from opensquilla.onboarding.image_generation_specs import (
        image_generation_provider_catalog_payload,
    )
    from opensquilla.onboarding.memory_embedding_specs import (
        memory_embedding_provider_catalog_payload,
    )
    from opensquilla.onboarding.provider_specs import provider_catalog_payload
    from opensquilla.onboarding.router_specs import router_catalog_payload
    from opensquilla.onboarding.search_specs import search_provider_catalog_payload

    return {
        "providers": provider_catalog_payload(),
        "channels": channel_catalog_payload(),
        "searchProviders": search_provider_catalog_payload(),
        "routerProfiles": router_catalog_payload(),
        "memoryEmbeddingProviders": memory_embedding_provider_catalog_payload(),
        "imageGenerationProviders": image_generation_provider_catalog_payload(),
        "audioProviders": audio_provider_catalog_payload(),
    }


def _require(params: Any, key: str) -> Any:
    if not isinstance(params, dict) or key not in params:
        raise ValueError(f"params.{key} is required")
    return params[key]


def _param(params: Any, key: str, default: Any) -> Any:
    """``params.get`` that also maps an explicit JSON ``null`` to ``default``.

    The onboarding mutations widened several parameters to ``None`` =
    keep-current for the CLI, but over RPC the legacy contract is pinned:
    an absent key AND an explicit ``null`` both mean the legacy default
    (reset/derive/clear), so hand-written clients sending ``null`` keep the
    pre-widening behavior instead of silently keeping stored values.
    """
    if not isinstance(params, dict):
        return default
    value = params.get(key, default)
    return default if value is None else value


def _bool_param(params: Any, key: str, default: bool = False) -> bool:
    value = _param(params, key, default)
    if not isinstance(value, bool):
        raise ValueError(f"params.{key} must be a boolean")
    return value


def _provider_candidate_identity(
    cfg: Any,
    provider_id: str,
    candidate_base_url: str,
) -> tuple[bool, bool]:
    """Return ``(same_provider, stored_credentials_may_be_reused)``."""
    from opensquilla.onboarding.endpoint_identity import (
        base_url_allows_credential_reuse,
    )

    active_provider = str(getattr(cfg.llm, "provider", "") or "").strip().lower()
    requested_provider = str(provider_id or "").strip().lower()
    same_provider = active_provider == requested_provider
    return same_provider, same_provider and base_url_allows_credential_reuse(
        str(getattr(cfg.llm, "base_url", "") or ""),
        candidate_base_url,
    )


def _request_changes_active_provider_connection(params: Any, cfg: Any) -> bool:
    """Return whether explicitly supplied connection fields differ from active.

    The Web UI may echo the saved Base URL and proxy when asking for a manual
    refresh.  Presence alone does not make that request an unsaved draft: only
    a different effective value must keep entitlement data ephemeral.
    """

    if not isinstance(params, dict):
        return False
    llm = getattr(cfg, "llm", None)
    from opensquilla.endpoint_identity import base_url_matches_official_api
    from opensquilla.provider.tokenrhythm_catalog import (
        canonical_tokenrhythm_base_url,
    )

    requested_provider = str(
        params.get("providerId") or getattr(llm, "provider", "") or ""
    ).strip().lower()

    comparisons = (
        ("apiKey", "api_key"),
        ("apiKeyEnv", "api_key_env"),
        ("baseUrl", "base_url"),
        ("proxy", "proxy"),
    )
    for rpc_field, config_field in comparisons:
        raw_value = params.get(rpc_field)
        if raw_value is None:
            continue
        candidate = str(raw_value or "").strip()
        if rpc_field == "apiKey" and is_redacted_secret_sentinel(candidate):
            continue
        # Blank discovery/probe fields retain the saved value.
        if not candidate:
            continue
        active = str(getattr(llm, config_field, "") or "").strip()
        if rpc_field == "baseUrl":
            # Treat scheme/host casing, an explicit default port, and a
            # trailing slash as the same deployment while keeping the API
            # path, query, fragment, and user-info boundary fail-closed.
            if requested_provider == "tokenrhythm":
                active_identity = canonical_tokenrhythm_base_url(active)
                candidate_identity = canonical_tokenrhythm_base_url(candidate)
                if not active_identity or candidate_identity != active_identity:
                    return True
            elif not base_url_matches_official_api(active, candidate):
                return True
        elif candidate != active:
            return True
    return False


def _llm_profile_for(config: Any, provider_id: str) -> Any | None:
    provider = str(provider_id or "").strip().lower()
    for key, profile in (getattr(config, "llm_profiles", None) or {}).items():
        if str(key or "").strip().lower() == provider:
            return profile
    return None


def _llm_profile_credential_signature(config: Any, provider_id: str) -> tuple[object, ...]:
    """Return the in-memory credential-source shape for pool invalidation."""

    profile = _llm_profile_for(config, provider_id)
    if profile is None:
        return ()
    return (
        str(getattr(profile, "api_key", "") or ""),
        str(getattr(profile, "api_key_env", "") or ""),
        tuple(getattr(profile, "api_key_env_pool", None) or ()),
    )


async def _reconcile_saved_llm_profile(
    previous_config: Any,
    current_config: Any,
    provider_id: str,
) -> None:
    from opensquilla.gateway.model_catalog_refresh import (
        reconcile_tokenrhythm_profile_transition,
    )

    await reconcile_tokenrhythm_profile_transition(
        previous_config,
        current_config,
        provider_id=provider_id,
    )


@_d.method("onboarding.provider.configure", scope="operator.admin")
async def _provider_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.mutations import upsert_llm_provider

    provider_id = _require(params, "providerId")
    # Legacy null semantics pinned: absent key OR explicit null = legacy
    # default ("" -> derive/reset), never keep-current (see _param).
    model = _param(params, "model", "")
    cfg = _active_config(ctx)
    with _validation_error("onboarding.provider.invalid"):
        res = upsert_llm_provider(
            cfg,
            provider_id=provider_id,
            model=model,
            api_key=_param(params, "apiKey", ""),
            api_key_env=_param(params, "apiKeyEnv", ""),
            preserve_api_key=_bool_param(params, "preserveApiKey"),
            base_url=_param(params, "baseUrl", ""),
            proxy=_param(params, "proxy", ""),
            # Explicit-user-action only (D18): a preset is applied exactly when
            # the client sends presetId; a plain save never auto-applies one.
            preset_id=_param(params, "presetId", ""),
            router_action=_param(params, "routerAction", "preserve"),
            image_generation_intent=_param(
                params,
                "imageGenerationIntent",
                "preserve",
            ),
        )
    # Persist first: if the write fails, the live config is untouched and
    # memory/disk stay consistent. Tool syncs run only on applied state.
    config_path = _persist(ctx, res.config, restart_required=res.restart_required)
    _apply_inplace(ctx, res.config)
    _sync_provider_selector(ctx, res.config.llm)
    _sync_image_generation(res.config)
    # Provider saves are an explicit retry boundary for registry-declared
    # public model listings. Await the bounded best-effort refresh so the next
    # turn observes the new catalog without requiring a gateway restart.
    from opensquilla.gateway.model_catalog_refresh import refresh_live_model_catalog

    await refresh_live_model_catalog(ctx.config if ctx.config is not None else res.config)
    return {
        "changed": res.changed,
        "restartRequired": res.restart_required,
        "configPath": config_path,
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.llmProfile.upsert", scope="operator.admin")
async def _llm_profile_upsert(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Create/update a non-primary provider profile without exposing its secret."""
    from opensquilla.onboarding.mutations import upsert_llm_profile

    provider_id = _require(params, "providerId")
    p = params if isinstance(params, dict) else {}
    pool = p.get("apiKeyEnvPool") if "apiKeyEnvPool" in p else None
    if pool is not None and not isinstance(pool, list):
        raise RpcHandlerError(
            "onboarding.llmProfile.invalid",
            "params.apiKeyEnvPool must be an array of environment-variable names",
        )
    preserve_value = p.get("keepCurrentSecret", p.get("preserveApiKey", False))
    if not isinstance(preserve_value, bool):
        raise RpcHandlerError(
            "onboarding.llmProfile.invalid",
            "params.keepCurrentSecret must be a boolean",
        )
    cfg = _active_config(ctx)
    previous_config = cfg.model_copy(deep=True)
    with _validation_error("onboarding.llmProfile.invalid"):
        res = upsert_llm_profile(
            cfg,
            provider_id=str(provider_id),
            model=p.get("model") if "model" in p else None,
            api_key=p.get("apiKey") if "apiKey" in p else None,
            api_key_env=p.get("apiKeyEnv") if "apiKeyEnv" in p else None,
            api_key_env_pool=pool,
            preserve_api_key=preserve_value,
            base_url=p.get("baseUrl") if "baseUrl" in p else None,
            proxy=p.get("proxy") if "proxy" in p else None,
        )
    credential_source_changed = _llm_profile_credential_signature(
        cfg, str(provider_id)
    ) != _llm_profile_credential_signature(res.config, str(provider_id))
    config_path = _persist(ctx, res.config, restart_required=res.restart_required)
    _apply_inplace(ctx, res.config)
    if credential_source_changed:
        from opensquilla.gateway.llm_runtime import discard_profile_credential_pool

        discard_profile_credential_pool(str(provider_id))
    await _reconcile_saved_llm_profile(previous_config, res.config, str(provider_id))
    return {
        "changed": res.changed,
        "restartRequired": res.restart_required,
        "configPath": config_path,
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.llmProfile.credential.clear", scope="operator.admin")
async def _llm_profile_credential_clear(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Clear stored profile credentials without removing the profile."""
    from opensquilla.gateway.llm_runtime import discard_profile_credential_pool
    from opensquilla.onboarding.mutations import clear_llm_profile_credentials

    provider_id = str(_require(params, "providerId"))
    cfg = _active_config(ctx)
    previous_config = cfg.model_copy(deep=True)
    backup_redaction = _provider_backup_credential_redaction(provider_id)
    with _validation_error("onboarding.llmProfile.invalid"):
        res = clear_llm_profile_credentials(cfg, provider_id=provider_id)
    config_path = _persist(
        ctx,
        res.config,
        restart_required=res.restart_required,
        backup_credential_redaction=backup_redaction,
    )
    _apply_inplace(ctx, res.config)
    # A configured rotation pool holds resolved key values, cooldowns and
    # session pins in process memory. Purge that provider only after disk is
    # committed and the live config is updated.
    discard_profile_credential_pool(provider_id)
    await _reconcile_saved_llm_profile(previous_config, res.config, provider_id)
    _sync_image_generation(res.config)
    entry = {
        **res.public_payload,
        **_credential_clear_effective_payload(
            ctx.config if ctx.config is not None else res.config,
            provider_id,
            active=False,
        ),
    }
    return {
        "changed": res.changed,
        "restartRequired": res.restart_required,
        "configPath": config_path,
        "entry": entry,
        "warnings": res.warnings,
    }


@_d.method("onboarding.llmProfile.remove", scope="operator.admin")
async def _llm_profile_remove(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Remove a profile only when no Router/Ensemble deployment references it."""
    from opensquilla.gateway.llm_runtime import discard_profile_credential_pool
    from opensquilla.onboarding.mutations import remove_llm_profile

    provider_id = _require(params, "providerId")
    cfg = _active_config(ctx)
    previous_config = cfg.model_copy(deep=True)
    with _validation_error("onboarding.llmProfile.invalid"):
        res = remove_llm_profile(cfg, provider_id=str(provider_id))
    config_path = _persist(ctx, res.config, restart_required=res.restart_required)
    _apply_inplace(ctx, res.config)
    discard_profile_credential_pool(str(provider_id))
    await _reconcile_saved_llm_profile(previous_config, res.config, str(provider_id))
    return {
        "changed": res.changed,
        "restartRequired": res.restart_required,
        "configPath": config_path,
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.llmProfile.active.remove", scope="operator.admin")
async def _llm_profile_active_remove(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Atomically replace and remove the current primary provider."""
    from opensquilla.onboarding.mutations import (
        LlmProfileActivationError,
        LlmProfileRemovalError,
        remove_active_llm_profile,
    )

    provider_id = str(_require(params, "providerId"))
    replacement_provider_id = str(_require(params, "replacementProviderId"))
    replacement_model = str(_param(params, "replacementModel", "") or "")
    router_action = str(_param(params, "routerAction", "preserve"))
    image_generation_intent = str(
        _param(params, "imageGenerationIntent", "preserve")
    )
    cfg = _active_config(ctx)
    previous_config = cfg.model_copy(deep=True)
    try:
        res = remove_active_llm_profile(
            cfg,
            provider_id=provider_id,
            replacement_provider_id=replacement_provider_id,
            replacement_model=replacement_model,
            router_action=router_action,
            image_generation_intent=image_generation_intent,
        )
    except LlmProfileActivationError as exc:
        code_by_reason = {
            "primary_pool_unsupported": (
                "onboarding.llmProfile.primary_pool_unsupported"
            ),
            "router_provider_conflict": (
                "onboarding.llmProfile.router_provider_conflict"
            ),
        }
        raise RpcHandlerError(
            code_by_reason.get(exc.reason, "onboarding.llmProfile.invalid"),
            str(exc),
            details={
                "reason": exc.reason,
                "providerId": provider_id.strip().lower(),
                "replacementProviderId": replacement_provider_id.strip().lower(),
                **exc.details,
            },
        ) from exc
    except LlmProfileRemovalError as exc:
        code_by_reason = {
            "active_mismatch": "onboarding.llmProfile.active_mismatch",
            "profile_referenced": "onboarding.llmProfile.referenced",
        }
        raise RpcHandlerError(
            code_by_reason.get(exc.reason, "onboarding.llmProfile.invalid"),
            str(exc),
            details={
                "reason": exc.reason,
                "providerId": provider_id.strip().lower(),
                "replacementProviderId": replacement_provider_id.strip().lower(),
                **exc.details,
            },
        ) from exc
    except (ValueError, KeyError) as exc:
        raise RpcHandlerError("onboarding.llmProfile.invalid", str(exc)) from exc

    # This is the sole transaction boundary for the composite mutation.
    # Activation/removal remain pure until the complete candidate is durable.
    config_path = _persist(ctx, res.config, restart_required=res.restart_required)
    _apply_inplace(ctx, res.config)
    from opensquilla.gateway.llm_runtime import discard_profile_credential_pool

    discard_profile_credential_pool(provider_id)
    await _reconcile_saved_llm_profile(previous_config, res.config, provider_id)
    _sync_provider_selector(ctx, res.config.llm)
    _sync_image_generation(res.config)
    from opensquilla.gateway.model_catalog_refresh import refresh_live_model_catalog

    await refresh_live_model_catalog(ctx.config if ctx.config is not None else res.config)
    return {
        "changed": res.changed,
        "restartRequired": res.restart_required,
        "configPath": config_path,
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.llmProfile.activate", scope="operator.admin")
async def _llm_profile_activate(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Promote one stored profile without moving secrets through the client."""
    from opensquilla.onboarding.mutations import (
        LlmProfileActivationError,
        activate_llm_profile,
    )

    provider_id = str(_require(params, "providerId"))
    model = str(_param(params, "model", "") or "")
    router_action = _param(params, "routerAction", "preserve")
    image_generation_intent = _param(
        params,
        "imageGenerationIntent",
        "preserve",
    )
    cfg = _active_config(ctx)
    try:
        res = activate_llm_profile(
            cfg,
            provider_id=provider_id,
            model=model,
            router_action=str(router_action),
            image_generation_intent=str(image_generation_intent),
        )
    except LlmProfileActivationError as exc:
        code_by_reason = {
            "primary_pool_unsupported": (
                "onboarding.llmProfile.primary_pool_unsupported"
            ),
            "router_provider_conflict": (
                "onboarding.llmProfile.router_provider_conflict"
            ),
        }
        code = code_by_reason.get(exc.reason, "onboarding.llmProfile.invalid")
        details = {
            "reason": exc.reason,
            "providerId": provider_id.strip().lower(),
            **exc.details,
        }
        raise RpcHandlerError(
            code,
            str(exc),
            details=details,
        ) from exc
    except (ValueError, KeyError) as exc:
        raise RpcHandlerError("onboarding.llmProfile.invalid", str(exc)) from exc

    # Disk commit is the transaction boundary. No selector/media/catalog
    # update is attempted when persistence fails.
    config_path = _persist(ctx, res.config, restart_required=res.restart_required)
    _apply_inplace(ctx, res.config)
    _sync_provider_selector(ctx, res.config.llm)
    _sync_image_generation(res.config)
    from opensquilla.gateway.model_catalog_refresh import refresh_live_model_catalog

    await refresh_live_model_catalog(ctx.config if ctx.config is not None else res.config)
    return {
        "changed": res.changed,
        "restartRequired": res.restart_required,
        "configPath": config_path,
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


def _llm_profile_rpc_session_key(ctx: RpcContext, provider_id: str) -> str:
    provider = str(provider_id or "").strip().lower()
    return f"onboarding-profile-rpc:{ctx.conn_id}:{provider}"


def _resolved_llm_profile_config(
    cfg: Any,
    provider_id: str,
    model: str,
    *,
    session_key: str,
) -> Any:
    """Resolve a stored profile or raise a stable, secret-free validation error."""
    from opensquilla.engine.selector_override import acquire_profile_credential
    from opensquilla.provider.deployment import resolve_provider_deployment

    provider = str(provider_id or "").strip().lower()
    profiles = getattr(cfg, "llm_profiles", None) or {}
    if not any(str(key or "").strip().lower() == provider for key in profiles):
        raise ValueError(f"provider profile {provider!r} does not exist")
    resolution = resolve_provider_deployment(
        cfg,
        provider,
        model,
        session_key=session_key,
        credential_pool_acquirer=acquire_profile_credential,
    )
    if not resolution.ready or resolution.provider_config is None:
        raise ValueError(
            f"provider profile {resolution.provider!r} is not executable: {resolution.reason}"
        )
    return resolution


def _report_llm_profile_rpc_failure(
    provider_id: str,
    session_key: str,
    failure_kind: str,
) -> None:
    """Park a failed pooled credential; non-pool/profile failures are no-ops."""
    if not failure_kind:
        return
    from opensquilla.engine.selector_override import report_profile_credential_failure
    from opensquilla.provider.failures import ProviderFailureKind

    try:
        kind = ProviderFailureKind(failure_kind)
    except ValueError:
        return
    report_profile_credential_failure(provider_id, session_key, kind)


def _draft_llm_profile_config(params: Any, ctx: RpcContext) -> tuple[str, Any]:
    """Build an in-memory profile draft without persisting or hot-applying it."""
    from opensquilla.onboarding.mutations import upsert_llm_profile

    provider_id = str(_require(params, "providerId"))
    provider = provider_id.strip().lower()
    p = params if isinstance(params, dict) else {}
    preserve_value = p.get("keepCurrentSecret", True)
    if not isinstance(preserve_value, bool):
        raise ValueError("params.keepCurrentSecret must be a boolean")
    cfg = _active_config(ctx)
    profiles = getattr(cfg, "llm_profiles", None) or {}
    if not any(str(key or "").strip().lower() == provider for key in profiles):
        raise ValueError(f"provider profile {provider!r} does not exist")
    draft = upsert_llm_profile(
        cfg,
        provider_id=provider,
        api_key=p.get("apiKey") if "apiKey" in p else None,
        api_key_env=p.get("apiKeyEnv") if "apiKeyEnv" in p else None,
        preserve_api_key=preserve_value,
        base_url=p.get("baseUrl") if "baseUrl" in p else None,
        proxy=p.get("proxy") if "proxy" in p else None,
    )
    return provider, draft.config


async def _usage_accounted_provider_probe(
    ctx: RpcContext,
    *,
    provider_id: str,
    model: str,
    api_key: str,
    api_key_env: str,
    base_url: str,
    proxy: str,
    allow_default_api_key_env: bool,
) -> ProviderProbeResult:
    """Probe one deployment under the shared physical-call usage boundary."""
    import uuid

    from opensquilla.engine.usage_accounting import (
        UsageAccountingScope,
        UsageExecutionContext,
        account_provider_stream,
        bind_usage_accounting_scope,
        provider_accounts_physical_usage,
    )
    from opensquilla.onboarding.probe import probe_llm_provider

    usage_scope = None
    chat_stream_factory = None
    if ctx.usage_event_sink is not None:
        execution_id = uuid.uuid4().hex
        usage_scope = UsageAccountingScope(
            sink=ctx.usage_event_sink,
            context=UsageExecutionContext(
                execution_id=execution_id,
                agent_run_id=execution_id,
                turn_id=execution_id,
                session_id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "opensquilla:system:onboarding-provider-probe",
                ).hex,
                agent_id="system",
                run_kind="onboarding_probe",
            ),
        )

        def chat_stream_factory(provider: Any, messages: Any, chat_config: Any) -> Any:
            if provider_accounts_physical_usage(provider):
                return provider.chat(messages, config=chat_config)
            return account_provider_stream(
                lambda: provider.chat(messages, config=chat_config),
                provider=str(provider_id),
                model=str(model),
            )

    probe_kwargs: dict[str, Any] = {
        "provider_id": provider_id,
        "model": model,
        "api_key": api_key,
        "api_key_env": api_key_env,
        "base_url": base_url,
        "proxy": proxy,
        "allow_default_api_key_env": allow_default_api_key_env,
    }
    if chat_stream_factory is not None:
        probe_kwargs["chat_stream_factory"] = chat_stream_factory
    with bind_usage_accounting_scope(usage_scope):
        return await probe_llm_provider(**probe_kwargs)


@_d.method("onboarding.llmProfile.probe", scope="operator.admin")
async def _llm_profile_probe(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Run a small live probe using the stored profile's resolved deployment."""
    provider_id = str(_require(params, "providerId"))
    model = str(_require(params, "model") or "").strip()
    cfg = _active_config(ctx)
    session_key = _llm_profile_rpc_session_key(ctx, provider_id)
    with _validation_error("onboarding.llmProfile.invalid"):
        resolution = _resolved_llm_profile_config(
            cfg,
            provider_id,
            model,
            session_key=session_key,
        )
        deployment = resolution.provider_config
        result = await _usage_accounted_provider_probe(
            ctx,
            provider_id=deployment.provider,
            model=deployment.model,
            api_key=deployment.api_key,
            api_key_env="",
            base_url=deployment.base_url,
            proxy=deployment.proxy,
            allow_default_api_key_env=False,
        )
        if not result.ok and resolution.credential_source == "profile_pool":
            _report_llm_profile_rpc_failure(
                deployment.provider,
                session_key,
                result.failure_kind,
            )
    from opensquilla.onboarding.probe_history import record_probe

    record_probe(cfg, deployment.provider, ok=result.ok, failure_kind=result.failure_kind)
    return result.to_payload()


@_d.method("onboarding.llmProfile.draft.probe", scope="operator.admin")
async def _llm_profile_draft_probe(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Probe the editor's current profile draft without saving any field."""
    model = str(_require(params, "model") or "").strip()
    with _validation_error("onboarding.llmProfile.invalid"):
        provider_id, draft = _draft_llm_profile_config(params, ctx)
        session_key = _llm_profile_rpc_session_key(ctx, provider_id)
        resolution = _resolved_llm_profile_config(
            draft,
            provider_id,
            model,
            session_key=session_key,
        )
        deployment = resolution.provider_config
        result = await _usage_accounted_provider_probe(
            ctx,
            provider_id=deployment.provider,
            model=deployment.model,
            api_key=deployment.api_key,
            api_key_env="",
            base_url=deployment.base_url,
            proxy=deployment.proxy,
            allow_default_api_key_env=False,
        )
        if not result.ok and resolution.credential_source == "profile_pool":
            _report_llm_profile_rpc_failure(
                deployment.provider,
                session_key,
                result.failure_kind,
            )
    # Do not return request fields or the cloned config: both may contain keys.
    return result.to_payload()


@_d.method("onboarding.llmProfile.models.discover", scope="operator.admin")
async def _llm_profile_models_discover(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Discover picker-safe models through one stored profile deployment."""
    from opensquilla.onboarding.probe import discover_selectable_provider_models

    provider_id = str(_require(params, "providerId"))
    cfg = _active_config(ctx)
    placeholder_model = str(getattr(cfg.llm, "model", "") or "profile-discovery")
    session_key = _llm_profile_rpc_session_key(ctx, provider_id)
    with _validation_error("onboarding.llmProfile.invalid"):
        resolution = _resolved_llm_profile_config(
            cfg,
            provider_id,
            placeholder_model,
            session_key=session_key,
        )
        deployment = resolution.provider_config
        result = await discover_selectable_provider_models(
            provider_id=deployment.provider,
            api_key=deployment.api_key,
            api_key_env="",
            base_url=deployment.base_url,
            proxy=deployment.proxy,
            allow_default_api_key_env=False,
            force_refresh=_bool_param(params, "forceRefresh"),
            persist_catalog=True,
            catalog_config=cfg,
        )
        if not result.ok and resolution.credential_source == "profile_pool":
            _report_llm_profile_rpc_failure(
                deployment.provider,
                session_key,
                result.failure_kind,
            )
    return result.to_payload()


@_d.method("onboarding.llmProfile.draft.models.discover", scope="operator.admin")
async def _llm_profile_draft_models_discover(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Discover models through the editor's unsaved profile deployment."""
    from opensquilla.onboarding.probe import discover_selectable_provider_models

    with _validation_error("onboarding.llmProfile.invalid"):
        provider_id, draft = _draft_llm_profile_config(params, ctx)
        placeholder_model = str(getattr(draft.llm, "model", "") or "profile-discovery")
        session_key = _llm_profile_rpc_session_key(ctx, provider_id)
        resolution = _resolved_llm_profile_config(
            draft,
            provider_id,
            placeholder_model,
            session_key=session_key,
        )
        deployment = resolution.provider_config
        result = await discover_selectable_provider_models(
            provider_id=deployment.provider,
            api_key=deployment.api_key,
            api_key_env="",
            base_url=deployment.base_url,
            proxy=deployment.proxy,
            allow_default_api_key_env=False,
            force_refresh=_bool_param(params, "forceRefresh"),
            persist_catalog=False,
            catalog_config=draft,
        )
        if not result.ok and resolution.credential_source == "profile_pool":
            _report_llm_profile_rpc_failure(
                deployment.provider,
                session_key,
                result.failure_kind,
            )
    return result.to_payload()


@_d.method("onboarding.provider.probe", scope="operator.admin")
async def _provider_probe(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Live one-token probe of a candidate provider config (nothing is saved)."""
    provider_id = _require(params, "providerId")
    p = params if isinstance(params, dict) else {}
    cfg = _active_config(ctx)
    api_key = str(p.get("apiKey", "") or "")
    if is_redacted_secret_sentinel(api_key):
        # A round-tripped redaction mask is a display value, not a
        # credential: fall through to the stored-credential reuse below
        # instead of probing with a literal '***' bearer token.
        api_key = ""
    api_key_env = str(p.get("apiKeyEnv", "") or "")
    base_url = str(p.get("baseUrl", "") or "")
    proxy = str(p.get("proxy", "") or "")
    # Draft probes carry explicit fields; only a bare providerId(+model)
    # request verifies the saved deployment and may update probe history.
    request_overrides = _request_changes_active_provider_connection(p, cfg)
    # A provider id is not an endpoint identity for configurable providers.
    # Stored credentials may follow an omitted URL or a same-origin path
    # change, but never a scheme/host/effective-port change.
    same_provider, reuse_stored_credentials = _provider_candidate_identity(
        cfg,
        str(provider_id),
        base_url,
    )
    if same_provider:
        if not api_key and not api_key_env and reuse_stored_credentials:
            api_key = str(getattr(cfg.llm, "api_key", "") or "")
            api_key_env = str(getattr(cfg.llm, "api_key_env", "") or "")
        if not base_url:
            base_url = str(getattr(cfg.llm, "base_url", "") or "")
        if not proxy:
            proxy = str(getattr(cfg.llm, "proxy", "") or "")
    model = str(p.get("model", "") or "")
    with _validation_error("onboarding.provider.invalid"):
        result = await _usage_accounted_provider_probe(
            ctx,
            provider_id=str(provider_id),
            model=model,
            api_key=api_key,
            api_key_env=api_key_env,
            base_url=base_url,
            proxy=proxy,
            allow_default_api_key_env=(
                not same_provider or reuse_stored_credentials
            ),
        )
    saved_model = str(getattr(cfg.llm, "model", "") or "").strip()
    if (
        same_provider
        and reuse_stored_credentials
        and not request_overrides
        and (not model.strip() or model.strip() == saved_model)
    ):
        from opensquilla.onboarding.probe_history import record_probe

        record_probe(
            cfg,
            str(provider_id),
            ok=bool(getattr(result, "ok", False)),
            failure_kind=str(getattr(result, "failure_kind", "") or ""),
        )
    return result.to_payload()


@_d.method("onboarding.provider.credential.reveal", scope="operator.admin")
async def _provider_credential_reveal(params: Any, ctx: RpcContext) -> dict[str, Any]:
    provider_id = _require(params, "providerId")
    return _active_llm_credential_reveal_payload(ctx, provider_id)


@_d.method("onboarding.provider.credential.clear", scope="operator.admin")
async def _provider_credential_clear(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Clear stored credentials for the active provider, preserving its setup."""
    from opensquilla.onboarding.mutations import clear_llm_provider_credentials

    provider_id = str(_require(params, "providerId"))
    cfg = _active_config(ctx)
    backup_redaction = _provider_backup_credential_redaction(provider_id)
    with _validation_error("onboarding.provider.invalid"):
        res = clear_llm_provider_credentials(cfg, provider_id=provider_id)
    # Keep the same transaction boundary as provider.configure: no runtime
    # consumer sees the cleared value until the durable write succeeds.
    config_path = _persist(
        ctx,
        res.config,
        restart_required=res.restart_required,
        backup_credential_redaction=backup_redaction,
    )
    _apply_inplace(ctx, res.config)
    _sync_provider_selector(ctx, res.config.llm)
    live_config = ctx.config if ctx.config is not None else res.config
    # Selector sync may resolve the provider's registry-default environment
    # key on a scratch config. The selector may keep using that external key,
    # but the cleared live config itself holds no cached secret and therefore
    # must not retain stale runtime-secret provenance.
    if not str(getattr(live_config.llm, "api_key", "") or ""):
        live_config._runtime_secret_paths.discard("llm.api_key")
    _sync_image_generation(res.config)
    from opensquilla.gateway.model_catalog_refresh import refresh_live_model_catalog

    await refresh_live_model_catalog(live_config)
    entry = {
        **res.public_payload,
        **_credential_clear_effective_payload(
            live_config,
            provider_id,
            active=True,
        ),
    }
    return {
        "changed": res.changed,
        "restartRequired": res.restart_required,
        "configPath": config_path,
        "entry": entry,
        "warnings": res.warnings,
    }


@_d.method("onboarding.models.discover", scope="operator.admin")
async def _models_discover(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """List verified picker-safe models without persisting anything.

    Admin-scoped (like ``onboarding.provider.probe``): the request carries
    candidate credentials, so it must not be reachable at the read/write
    tiers even though it changes no state.

    Selector discovery is fail-closed: only registry-verified providers on
    their official hosts are queried. Self-hosted and arbitrary endpoints
    remain manual-entry surfaces; raw CLI diagnostics retain their broader
    endpoint-probing behavior.

    Blank credentials fall back to the stored config's only while a supplied
    candidate Base URL remains same-origin; omitted Base URLs reuse the stored
    endpoint.
    """
    from opensquilla.onboarding.probe import discover_selectable_provider_models

    provider_id = _require(params, "providerId")
    p = params if isinstance(params, dict) else {}
    cfg = _active_config(ctx)
    api_key = str(p.get("apiKey", "") or "")
    if is_redacted_secret_sentinel(api_key):
        # Same keep-current boundary as onboarding.provider.probe: never
        # send a round-tripped '***' mask upstream as a bearer token.
        api_key = ""
    api_key_env = str(p.get("apiKeyEnv", "") or "")
    base_url = str(p.get("baseUrl", "") or "")
    proxy = str(p.get("proxy", "") or "")
    force_refresh = _bool_param(params, "forceRefresh")
    request_overrides = _request_changes_active_provider_connection(p, cfg)
    same_provider, reuse_stored_credentials = _provider_candidate_identity(
        cfg,
        str(provider_id),
        base_url,
    )
    if same_provider:
        if not api_key and not api_key_env and reuse_stored_credentials:
            api_key = str(getattr(cfg.llm, "api_key", "") or "")
            api_key_env = str(getattr(cfg.llm, "api_key_env", "") or "")
        if not base_url:
            base_url = str(getattr(cfg.llm, "base_url", "") or "")
        if not proxy:
            proxy = str(getattr(cfg.llm, "proxy", "") or "")
    with _validation_error("onboarding.provider.invalid"):
        result = await discover_selectable_provider_models(
            provider_id=provider_id,
            api_key=api_key,
            api_key_env=api_key_env,
            base_url=base_url,
            proxy=proxy,
            allow_default_api_key_env=(
                not same_provider or reuse_stored_credentials
            ),
            force_refresh=force_refresh,
            persist_catalog=(
                same_provider and reuse_stored_credentials and not request_overrides
            ),
            catalog_config=cfg,
        )
    return result.to_payload()


@_d.method("onboarding.imageGeneration.models.discover", scope="operator.admin")
async def _image_generation_models_discover(
    params: Any,
    ctx: RpcContext,
) -> dict[str, Any]:
    """List image-output-capable models without persisting configuration.

    Unlike the general LLM picker, this endpoint only uses provider image
    catalogs.  The live request, when supported, is fixed to the provider's
    official image-model endpoint and never accepts an operator-supplied URL or
    credential.  Curated setup-catalog rows provide an offline-safe fallback.
    """
    from opensquilla.onboarding.image_generation_model_discovery import (
        discover_image_generation_models,
    )

    provider_id = _require(params, "providerId")
    with _validation_error("onboarding.imageGeneration.invalid"):
        return await discover_image_generation_models(str(provider_id))


@_d.method("onboarding.router.catalog", scope="operator.read")
async def _router_catalog(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.router_specs import router_catalog_payload

    return router_catalog_payload()


@_d.method("onboarding.router.configure", scope="operator.admin")
async def _router_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.mutations import upsert_router

    cfg = _active_config(ctx)
    mode = params.get("mode", "recommended") if isinstance(params, dict) else "recommended"
    default_tier = params.get("defaultTier") if isinstance(params, dict) else None
    tiers = params.get("tiers") if isinstance(params, dict) else None
    cross_provider_tiers = params.get("crossProviderTiers") if isinstance(params, dict) else None
    tier_provider_mismatch = (
        params.get("tierProviderMismatch") if isinstance(params, dict) else None
    )
    with _validation_error("onboarding.router.invalid"):
        res = upsert_router(
            cfg,
            mode=mode,
            default_tier=default_tier,
            tiers=tiers,
            cross_provider_tiers=cross_provider_tiers,
            tier_provider_mismatch=tier_provider_mismatch,
        )
    # Persist first: if the write fails, the live config is untouched and
    # memory/disk stay consistent. Tool syncs run only on applied state.
    config_path = _persist(ctx, res.config, restart_required=res.restart_required)
    _apply_inplace(ctx, res.config)
    _sync_provider_selector(ctx, res.config.llm)
    await broadcast_model_routing_changed(
        ctx,
        source="onboarding.router.configure",
        config=res.config,
    )
    return {
        "changed": res.changed,
        "restartRequired": res.restart_required,
        "configPath": config_path,
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.ensemble.configure", scope="operator.admin")
async def _ensemble_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    """Configure the [llm_ensemble] routing surface.

    Omitted params keep the current value (partial-payload merge in the
    mutation); the TurnRunner reads llm_ensemble live, so no restart.
    """
    from opensquilla.onboarding.mutations import upsert_llm_ensemble

    cfg = _active_config(ctx)
    p = params if isinstance(params, dict) else {}
    with _validation_error("onboarding.ensemble.invalid"):
        res = upsert_llm_ensemble(
            cfg,
            enabled=p.get("enabled"),
            selection_mode=p.get("selectionMode"),
            model_options=p.get("modelOptions"),
            candidates=p.get("candidates"),
            min_successful_proposers=p.get("minSuccessfulProposers"),
            proposer_max_retries=p.get("proposerMaxRetries"),
            all_failed_policy=p.get("allFailedPolicy"),
        )
    # Persist first: if the write fails, the live config is untouched and
    # memory/disk stay consistent. Tool syncs run only on applied state.
    config_path = _persist(ctx, res.config, restart_required=res.restart_required)
    _apply_inplace(ctx, res.config)
    await broadcast_model_routing_changed(
        ctx,
        source="onboarding.ensemble.configure",
        config=res.config,
    )
    return {
        "changed": res.changed,
        "restartRequired": res.restart_required,
        "configPath": config_path,
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.channel.probe", scope="operator.admin")
async def _channel_probe(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.mutations import (
        merge_channel_entry_secrets,
        validate_channel_entry,
    )
    from opensquilla.onboarding.redaction import redact_channel_entry

    entry = _require(params, "entry")
    if not isinstance(entry, dict):
        raise ValueError("params.entry must be an object")
    # Merge-aware probe: blank secrets resolve against the stored entry the
    # same way onboarding.channel.upsert does, so probing a keep-current
    # payload validates the entry the upsert would actually persist instead
    # of hard-failing on the non-blank-secret requirement. A genuinely blank
    # secret (no stored entry to merge from) still fails validation.
    cfg = _active_config(ctx)
    with _channel_error():
        normalized = validate_channel_entry(merge_channel_entry_secrets(cfg, entry))
    type_name = str(normalized.get("type") or "")
    return {
        "status": "validated",
        "connected": False,
        "probeKind": "local_validation",
        "restartRequired": True,
        "entry": redact_channel_entry(type_name, normalized),
        "warnings": [
            "Configuration is locally valid; no provider connection was attempted."
        ],
    }


@_d.method("onboarding.search.configure", scope="operator.admin")
async def _search_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.mutations import upsert_search_provider

    provider_id = _require(params, "providerId")
    cfg = _active_config(ctx)
    with _validation_error("onboarding.search.invalid"):
        res = upsert_search_provider(
            cfg,
            provider_id=provider_id,
            # Legacy null semantics pinned: absent key OR explicit null maps
            # to the legacy default (reset/clear), never keep-current.
            api_key=_param(params, "apiKey", ""),
            api_key_env=_param(params, "apiKeyEnv", ""),
            max_results=_param(params, "maxResults", DEFAULT_SEARCH_MAX_RESULTS),
            proxy=_param(params, "proxy", ""),
            use_env_proxy=_param(params, "useEnvProxy", False),
            fallback_policy=_param(params, "fallbackPolicy", "off"),
            diagnostics=_param(params, "diagnostics", False),
        )
    # Persist first: if the write fails, the live config is untouched and
    # memory/disk stay consistent. Tool syncs run only on applied state.
    config_path = _persist(ctx, res.config, restart_required=res.restart_required)
    _apply_inplace(ctx, res.config)
    _sync_search_provider(res.config)
    return {
        "changed": res.changed,
        "restartRequired": res.restart_required,
        "configPath": config_path,
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.imageGeneration.configure", scope="operator.admin")
async def _image_generation_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.mutations import upsert_image_generation_provider

    provider_id = _require(params, "providerId")
    cfg = _active_config(ctx)
    fallbacks = params.get("fallbacks") if isinstance(params, dict) else None
    with _validation_error("onboarding.imageGeneration.invalid"):
        if fallbacks is not None and not isinstance(fallbacks, list):
            raise ValueError("fallbacks must be a list of provider/model references")
        res = upsert_image_generation_provider(
            cfg,
            provider_id=provider_id,
            primary=params.get("primary", "") if isinstance(params, dict) else "",
            api_key=params.get("apiKey", "") if isinstance(params, dict) else "",
            api_key_env=params.get("apiKeyEnv", "") if isinstance(params, dict) else "",
            base_url=params.get("baseUrl") if isinstance(params, dict) else None,
            enabled=params.get("enabled", True) if isinstance(params, dict) else True,
            size=params.get("size", "") if isinstance(params, dict) else "",
            output_format=params.get("outputFormat", "") if isinstance(params, dict) else "",
            fallbacks=list(fallbacks) if fallbacks is not None else None,
            clear_fallbacks=(
                params.get("clearFallbacks", False) if isinstance(params, dict) else False
            ),
            credential_mode=(
                params.get("credentialMode") if isinstance(params, dict) else None
            ),
        )
    # Persist first: if the write fails, the live config is untouched and
    # memory/disk stay consistent. Tool syncs run only on applied state.
    config_path = _persist(ctx, res.config, restart_required=res.restart_required)
    _apply_inplace(ctx, res.config)
    _sync_image_generation(res.config)
    return {
        "changed": res.changed,
        "restartRequired": res.restart_required,
        "configPath": config_path,
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.memory_embedding.configure", scope="operator.admin")
async def _memory_embedding_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.mutations import upsert_memory_embedding

    provider = _require(params, "providerId")
    cfg = _active_config(ctx)
    res = upsert_memory_embedding(
        cfg,
        provider=provider,
        model=params.get("model", "") if isinstance(params, dict) else "",
        api_key=params.get("apiKey", "") if isinstance(params, dict) else "",
        api_key_env=params.get("apiKeyEnv", "") if isinstance(params, dict) else "",
        base_url=params.get("baseUrl", "") if isinstance(params, dict) else "",
        onnx_dir=params.get("onnxDir", "") if isinstance(params, dict) else "",
    )
    # Persist first: if the write fails, the live config is untouched and
    # memory/disk stay consistent. Tool syncs run only on applied state.
    config_path = _persist(ctx, res.config, restart_required=res.restart_required)
    _apply_inplace(ctx, res.config)
    return {
        "changed": res.changed,
        "restartRequired": res.restart_required,
        "configPath": config_path,
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


def apply_audio_provider_configuration(
    config_holder: Any,
    *,
    provider_id: str,
    api_key: str = "",
    api_key_env: str = "",
    base_url: str = "",
    enabled: bool = True,
    tts_voice: str = "",
    tts_model: str = "",
    language_code: str = "",
) -> dict[str, Any]:
    """Validate, persist, and hot-apply one audio provider configuration.

    The single safe write path for audio config, shared by the
    ``onboarding.audio.configure`` RPC and the agent-facing ``audio_config``
    builtin tool. ``config_holder`` only needs a ``config`` attribute carrying
    the live ``GatewayConfig`` (an ``RpcContext``, or a shim for tools).

    The returned mapping is secret-safe: ``entry`` is the mutation's redacted
    public payload and never carries the API key.
    """
    from opensquilla.onboarding.mutations import upsert_audio_provider

    cfg = _active_config(config_holder)
    res = upsert_audio_provider(
        cfg,
        provider_id=provider_id,
        api_key=api_key,
        api_key_env=api_key_env,
        base_url=base_url,
        enabled=enabled,
        tts_voice=tts_voice,
        tts_model=tts_model,
        language_code=language_code,
    )
    # Persist first: if the write fails, the live config is untouched and
    # memory/disk stay consistent. Tool syncs run only on applied state.
    config_path = _persist(config_holder, res.config, restart_required=res.restart_required)
    _apply_inplace(config_holder, res.config)
    _sync_image_generation(res.config)
    return {
        "changed": res.changed,
        "restartRequired": res.restart_required,
        "configPath": config_path,
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


def apply_agent_audio_provider_configuration(
    config_holder: Any,
    *,
    provider_id: str,
    api_key: str = "",
    api_key_env: str = "",
    enabled: bool = True,
    tts_voice: str = "",
    tts_model: str = "",
    language_code: str = "",
) -> dict[str, Any]:
    """Apply the constrained audio configuration exposed to agents.

    Operator-facing RPCs may configure compatible endpoints and custom
    credential environment variables. The agent tool is deliberately pinned
    to the provider registry so it cannot redirect an unrelated environment
    credential to a model-selected endpoint.
    """
    from opensquilla.onboarding.audio_specs import get_audio_provider_setup_spec

    spec = get_audio_provider_setup_spec(provider_id)
    if api_key_env and api_key_env != spec.env_key:
        raise ValueError(
            f"audio provider {provider_id!r} only accepts api_key_env={spec.env_key!r} "
            "through this tool"
        )
    return apply_audio_provider_configuration(
        config_holder,
        provider_id=provider_id,
        api_key=api_key,
        api_key_env=api_key_env,
        base_url=spec.default_base_url,
        enabled=enabled,
        tts_voice=tts_voice,
        tts_model=tts_model,
        language_code=language_code,
    )


@_d.method("onboarding.audio.configure", scope="operator.admin")
async def _audio_configure(params: Any, ctx: RpcContext) -> dict[str, Any]:
    provider_id = _require(params, "providerId")
    p = params if isinstance(params, dict) else {}
    return apply_audio_provider_configuration(
        ctx,
        provider_id=provider_id,
        api_key=p.get("apiKey", ""),
        api_key_env=p.get("apiKeyEnv", ""),
        base_url=p.get("baseUrl", ""),
        enabled=p.get("enabled", True),
        tts_voice=p.get("ttsVoice", ""),
        tts_model=p.get("ttsModel", ""),
        language_code=p.get("languageCode", ""),
    )


@_d.method("onboarding.capability.reset", scope="operator.admin")
async def _capability_reset(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.mutations import reset_capability

    with _validation_error("onboarding.capability.invalid"):
        res = reset_capability(
            _active_config(ctx),
            capability_id=str(_require(params, "capabilityId")),
        )
    # Scrub the current config and all managed backups before swapping the
    # running config. Any persistence failure therefore leaves runtime intact.
    config_path = _persist(
        ctx,
        res.config,
        restart_required=res.restart_required,
        remove_paths=res.remove_paths,
    )
    _apply_inplace(ctx, res.config)
    canonical_capability_id = str(res.public_payload["capabilityId"])
    restart_required = res.restart_required
    warnings = list(res.warnings)
    try:
        if canonical_capability_id == "search":
            _sync_search_provider(res.config)
        elif canonical_capability_id in {"image_generation", "audio"}:
            _sync_image_generation(res.config)
    except Exception as exc:  # noqa: BLE001 - persisted reset degrades to restart
        restart_required = True
        warnings.append(
            "Capability reset was saved, but the live runtime could not be "
            "updated. Restart the gateway to apply it."
        )
        log.warning(
            "onboarding.capability_reset_live_sync_failed",
            capability_id=canonical_capability_id,
            error_type=type(exc).__name__,
        )
    return {
        "changed": res.changed,
        "restartRequired": restart_required,
        "configPath": config_path,
        "entry": res.public_payload,
        "warnings": warnings,
    }


async def _reconcile_channels_live() -> dict[str, str] | None:
    """Run the boot-registered channel reconciler against the live config.

    ``None`` means no reconciler is registered (standalone/config-only
    contexts) — everything stays restart-gated. A reconciler failure also
    degrades to restart-gated: the config is already persisted and applied
    in place, so the honest fallback is the pre-reconcile contract.
    """
    from opensquilla.gateway.channels_bridge import get_channels_reconciler

    reconciler = get_channels_reconciler()
    if reconciler is None:
        return None
    try:
        return await reconciler()
    except Exception as exc:  # noqa: BLE001 - config stays valid either way
        log.warning("onboarding.channel_reconcile_failed", error=str(exc))
        return None


def _live_apply_fields(live: dict[str, str] | None, names: list[str]) -> dict[str, Any]:
    """Response fields describing what the reconciler actually did.

    ``restartRequired`` stays the compatibility signal older clients read; it
    is scoped to the channel(s) THIS call mutated — an unrelated channel's
    outstanding restart must not relabel a live-applied save. ``failed`` does
    NOT flag a restart — restarting won't fix a bad entry; the channel
    carries its error in channels.status and channels.restart retries it.
    ``liveApply`` keeps the full per-name outcome map for observability.
    """
    if live is None:
        return {"restartRequired": True, "liveApply": None}
    pending = any(live.get(name) == "pending_restart" for name in names)
    return {"restartRequired": pending, "liveApply": live}


@_d.method("onboarding.channel.upsert", scope="operator.admin")
async def _channel_upsert(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.mutations import upsert_channel

    entry = _require(params, "entry")
    if not isinstance(entry, dict):
        raise ValueError("params.entry must be an object")
    cfg = _active_config(ctx)
    with _channel_error():
        res = upsert_channel(cfg, entry_payload=entry)
    # Persist first: if the write fails, the live config is untouched and
    # memory/disk stay consistent. Tool syncs run only on applied state.
    config_path = _persist(ctx, res.config, restart_required=True)
    _apply_inplace(ctx, res.config)
    live = await _reconcile_channels_live()
    entry_name = str(res.public_payload.get("name") or entry.get("name") or "")
    return {
        "changed": res.changed,
        **_live_apply_fields(live, [entry_name]),
        "configPath": config_path,
        "entry": res.public_payload,
        "warnings": res.warnings,
    }


@_d.method("onboarding.channel.remove", scope="operator.admin")
async def _channel_remove(params: Any, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.mutations import remove_channel

    name = _require(params, "name")
    cfg = _active_config(ctx)
    with _channel_error():
        res = remove_channel(cfg, name=name)
    # Persist first: if the write fails, the live config is untouched and
    # memory/disk stay consistent. Tool syncs run only on applied state.
    config_path = _persist(ctx, res.config, restart_required=True)
    _apply_inplace(ctx, res.config)
    live = await _reconcile_channels_live()
    return {
        "changed": res.changed,
        **_live_apply_fields(live, [name]),
        "configPath": config_path,
        "removed": name,
    }


async def _toggle(ctx: RpcContext, params: Any, enabled: bool) -> dict[str, Any]:
    from opensquilla.onboarding.mutations import set_channel_enabled

    name = _require(params, "name")
    cfg = _active_config(ctx)
    with _channel_error():
        res = set_channel_enabled(cfg, name=name, enabled=enabled)
    # Persist first: if the write fails, the live config is untouched and
    # memory/disk stay consistent. Tool syncs run only on applied state.
    config_path = _persist(ctx, res.config, restart_required=True)
    _apply_inplace(ctx, res.config)
    live = await _reconcile_channels_live()
    return {
        "changed": res.changed,
        **_live_apply_fields(live, [name]),
        "configPath": config_path,
        "name": name,
        "enabled": enabled,
    }


@_d.method("onboarding.channel.enable", scope="operator.admin")
async def _channel_enable(params: Any, ctx: RpcContext) -> dict[str, Any]:
    return await _toggle(ctx, params, True)


@_d.method("onboarding.channel.disable", scope="operator.admin")
async def _channel_disable(params: Any, ctx: RpcContext) -> dict[str, Any]:
    return await _toggle(ctx, params, False)
