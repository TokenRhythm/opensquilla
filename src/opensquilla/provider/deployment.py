"""Shared resolution of one configured provider/model deployment.

Router tiers and ensemble members both name a deployment as the pair
``(provider, model)``.  This module is the single place that turns that pair
into a runtime :class:`ProviderConfig`: provider-profile credentials,
credential pools, endpoints, and proxies must not drift between the two
execution paths.

The public result deliberately keeps the credential-bearing ProviderConfig
out of ``repr``.  Its provenance fields contain only source labels and, for
environment-backed credentials, the environment-variable *name*.
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any

import structlog

from opensquilla.endpoint_identity import base_url_allows_credential_reuse

from .environment import environment_value
from .registry import UnknownProviderError, get_provider_spec
from .selector import ProviderConfig

log = structlog.get_logger(__name__)

CredentialPoolAcquirer = Callable[[str, list[str], str], Any | None]
EnvironmentReader = Callable[[str], str]


class CredentialPoolExhaustedError(RuntimeError):
    """A configured profile pool has no credential currently available."""


@dataclass(frozen=True)
class ProviderDeploymentResolution:
    """Resolved runtime deployment plus non-secret readiness provenance."""

    provider: str
    model: str
    ready: bool
    reason: str = ""
    credential_source: str = "none"
    credential_env: str = ""
    endpoint_source: str = "none"
    proxy_source: str = "none"
    provider_config: ProviderConfig | None = field(default=None, repr=False)


def _profile_for(config: Any, provider_id: str) -> Any | None:
    profiles = getattr(config, "llm_profiles", None) or {}
    profile = profiles.get(provider_id)
    if profile is not None:
        return profile
    # Config keys authored by hand predate normalization at this boundary.
    # Accept a case variant without changing or persisting the user's config.
    for key, candidate in profiles.items():
        if str(key).strip().lower() == provider_id:
            return candidate
    return None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _unready(
    provider: str,
    model: str,
    reason: str,
    *,
    credential_source: str = "none",
    credential_env: str = "",
    endpoint_source: str = "none",
    proxy_source: str = "none",
    provider_config: ProviderConfig | None = None,
) -> ProviderDeploymentResolution:
    # Readiness/status surfaces call this resolver frequently; execution
    # boundaries add their own contextual warnings when they act on a failed
    # resolution, so the pure resolver remains quiet at normal log levels.
    log.debug("provider_deployment.unresolved", provider=provider, reason=reason)
    return ProviderDeploymentResolution(
        provider=provider,
        model=model,
        ready=False,
        reason=reason,
        credential_source=credential_source,
        credential_env=credential_env,
        endpoint_source=endpoint_source,
        proxy_source=proxy_source,
        provider_config=provider_config,
    )


def resolve_provider_deployment(
    config: Any,
    provider_id: str,
    model: str,
    *,
    inherited_provider_config: ProviderConfig | None = None,
    overrides: Any | None = None,
    session_key: str = "",
    turn_metadata: MutableMapping[str, Any] | None = None,
    replay_provider_state: bool | None = None,
    credential_pool_acquirer: CredentialPoolAcquirer | None = None,
    environment_reader: EnvironmentReader | None = None,
) -> ProviderDeploymentResolution:
    """Resolve a provider/model pair without guessing or leaking credentials.

    For a non-primary deployment, credential precedence is an explicit
    member override, then profile explicit key, profile key pool, profile env,
    and finally the provider registry env.  Endpoint precedence is an
    explicit member override, profile base URL, then the registry default.

    When the requested provider is the already-resolved inherited provider,
    that live config remains authoritative for compatibility with session
    overrides.  Profile resolution is for the non-primary deployment path.
    A global primary proxy remains the last proxy fallback, matching the
    existing router behavior.
    """
    provider = _text(provider_id).lower()
    model_id = _text(model)
    read_environment = environment_reader or environment_value
    if not provider:
        return _unready(provider, model_id, "unknown_provider")
    if not model_id:
        return _unready(provider, model_id, "missing_model")
    try:
        spec = get_provider_spec(provider)
    except UnknownProviderError:
        return _unready(provider, model_id, "unknown_provider")
    if not spec.runtime_supported:
        return _unready(provider, model_id, "runtime_unsupported")

    inherited_provider = _text(
        getattr(inherited_provider_config, "provider", "")
    ).lower()
    same_provider = bool(inherited_provider_config) and provider == inherited_provider
    profile = None if same_provider else _profile_for(config, provider)
    member_base_url = _text(getattr(overrides, "base_url", ""))
    profile_base_url = _text(getattr(profile, "base_url", ""))
    inherited_base_url = _text(getattr(inherited_provider_config, "base_url", ""))

    api_key = _text(getattr(overrides, "api_key", ""))
    credential_source = "member" if api_key else "none"
    credential_env = ""
    credential_endpoint = ""
    blocked_reason = ""

    if not api_key and same_provider:
        api_key = _text(getattr(inherited_provider_config, "api_key", ""))
        if api_key:
            credential_source = "inherited"
            credential_endpoint = inherited_base_url or _text(spec.default_base_url)

    if not api_key:
        api_key = _text(getattr(profile, "api_key", ""))
        if api_key:
            credential_source = "profile"
            credential_endpoint = profile_base_url or _text(spec.default_base_url)

    pool_names = [
        _text(name)
        for name in (getattr(profile, "api_key_env_pool", None) or [])
        if _text(name)
    ]
    if not api_key and pool_names:
        pooled = None
        if credential_pool_acquirer is not None:
            try:
                pooled = credential_pool_acquirer(provider, pool_names, session_key)
            except CredentialPoolExhaustedError:
                # Do not bypass a deliberately configured-but-exhausted pool
                # with another key source.  Still assemble a non-runnable
                # config below so status can inspect endpoint provenance.
                blocked_reason = "credential_pool_exhausted"
        else:
            # Readiness/catalog callers need a dependency-free, side-effect-
            # free view. Runtime callers inject the process-wide pool manager
            # so rotation, session pinning, and failure parking remain shared.
            for env_name in pool_names:
                candidate = read_environment(env_name).strip()
                if candidate:
                    api_key = candidate
                    credential_source = "profile_pool_env"
                    credential_env = env_name
                    credential_endpoint = profile_base_url or _text(spec.default_base_url)
                    break
        if pooled is not None:
            api_key = _text(getattr(pooled, "api_key", ""))
            credential_source = "profile_pool"
            credential_env = _text(getattr(pooled, "env_name", ""))
            credential_endpoint = profile_base_url or _text(spec.default_base_url)
            if turn_metadata is not None:
                # Non-secret identifiers only; used by the router failure hook
                # to park a rate-limited/invalid pooled credential.
                turn_metadata["credential_pool"] = {
                    "provider": provider,
                    "session_key": session_key,
                    "env_name": credential_env,
                    "key_id": _text(getattr(pooled, "key_id", "")),
                }

    member_env = _text(getattr(overrides, "api_key_env", ""))
    profile_env = _text(getattr(profile, "api_key_env", ""))
    if not api_key and not blocked_reason and member_env:
        api_key = read_environment(member_env).strip()
        if api_key:
            credential_source = "member_env"
            credential_env = member_env
    if not api_key and not blocked_reason and profile_env:
        api_key = read_environment(profile_env).strip()
        if api_key:
            credential_source = "profile_env"
            credential_env = profile_env
            credential_endpoint = profile_base_url or _text(spec.default_base_url)
    # The registry env key follows the registry-default endpoint origin. A
    # provider spec without a default base URL (azure-style: the endpoint is
    # operator-supplied by design) binds its env key to whatever endpoint the
    # operator configured, so the profile base URL does not gate it there.
    registry_env_matches_profile_endpoint = (
        not profile_base_url
        or not _text(spec.default_base_url)
        or base_url_allows_credential_reuse(spec.default_base_url, profile_base_url)
    )
    if (
        not api_key
        and not blocked_reason
        and registry_env_matches_profile_endpoint
        and spec.env_key
        and spec.env_key != "OAuth"
    ):
        api_key = read_environment(spec.env_key).strip()
        if api_key:
            credential_source = "registry_env"
            credential_env = spec.env_key
            credential_endpoint = _text(spec.default_base_url) or profile_base_url
    if spec.requires_api_key() and not api_key and not blocked_reason:
        blocked_reason = "missing_credential"
    if not spec.requires_api_key() and not api_key:
        credential_source = "keyless"

    base_url = member_base_url
    endpoint_source = "member" if base_url else "none"
    if not base_url and same_provider:
        base_url = _text(getattr(inherited_provider_config, "base_url", ""))
        if base_url:
            endpoint_source = "inherited"
    if not base_url:
        base_url = _text(getattr(profile, "base_url", ""))
        if base_url:
            endpoint_source = "profile"
    if not base_url:
        base_url = _text(spec.default_base_url)
        if base_url:
            endpoint_source = "registry"
    if not base_url and spec.requires_base_url() and not blocked_reason:
        blocked_reason = "missing_base_url"
    if (
        api_key
        and credential_endpoint
        and not base_url_allows_credential_reuse(credential_endpoint, base_url)
    ):
        # A credential inherited from the primary/profile/registry is bound
        # to that deployment's origin. An endpoint override needs its own
        # explicit member credential; never forward a reusable key to a new
        # origin merely because the provider id is unchanged.
        api_key = ""
        credential_source = "none"
        credential_env = ""
        blocked_reason = "credential_endpoint_mismatch"
        if turn_metadata is not None:
            turn_metadata.pop("credential_pool", None)

    proxy = _text(getattr(overrides, "proxy", ""))
    proxy_source = "member" if proxy else "none"
    if not proxy and same_provider:
        proxy = _text(getattr(inherited_provider_config, "proxy", ""))
        if proxy:
            proxy_source = "inherited"
    if not proxy:
        proxy = _text(getattr(profile, "proxy", ""))
        if proxy:
            proxy_source = "profile"
    if not proxy:
        proxy = _text(getattr(getattr(config, "llm", None), "proxy", ""))
        if proxy:
            proxy_source = "global"

    if replay_provider_state is None:
        replay = (
            bool(getattr(inherited_provider_config, "replay_provider_state", True))
            if same_provider
            else False
        )
    else:
        replay = bool(replay_provider_state)
    provider_routing = (
        dict(getattr(inherited_provider_config, "provider_routing", {}) or {})
        if same_provider
        else {}
    )
    provider_config = ProviderConfig(
        provider=provider,
        model=model_id,
        api_key=api_key,
        base_url=base_url,
        org_id=(
            _text(getattr(inherited_provider_config, "org_id", ""))
            if same_provider
            else ""
        ),
        proxy=proxy,
        provider_routing=provider_routing,
        replay_provider_state=replay,
    )
    if blocked_reason:
        return _unready(
            provider,
            model_id,
            blocked_reason,
            credential_source=credential_source,
            credential_env=credential_env,
            endpoint_source=endpoint_source,
            proxy_source=proxy_source,
            provider_config=provider_config,
        )
    return ProviderDeploymentResolution(
        provider=provider,
        model=model_id,
        ready=True,
        credential_source=credential_source,
        credential_env=credential_env,
        endpoint_source=endpoint_source,
        proxy_source=proxy_source,
        provider_config=provider_config,
    )
