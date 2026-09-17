"""Shared credential resolution for image-generation providers.

Image generation may own a dedicated credential, or it may borrow the
credential of the matching model-service deployment.  This module is the
single policy boundary used by onboarding status, mutations, and the runtime;
callers must never copy a borrowed secret into the image configuration.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast

from opensquilla.endpoint_identity import (
    base_url_allows_credential_reuse,
    credential_env_for_endpoint,
)
from opensquilla.provider.environment import environment_value
from opensquilla.provider.failures import ProviderFailureKind

ImageCredentialSource = Literal[
    "explicit",
    "env",
    "llm_fallback",
    "missing_env",
    "none",
]
ImageCredentialOwner = Literal["image", "primary", "profile", "none"]
ImageCredentialKind = Literal["direct", "env", "pool", "none"]
PoolFailureReporter = Callable[
    [str, str, ProviderFailureKind, float | None],
    None,
]


class _ModelCopyConfig(Protocol):
    def model_copy(self, *, deep: bool = False) -> object: ...


@dataclass(frozen=True)
class ImageGenerationCredentialResolution:
    """One resolved image credential with secret-safe public metadata."""

    provider_id: str
    source: ImageCredentialSource
    owner: ImageCredentialOwner
    kind: ImageCredentialKind
    available: bool
    env_key: str = ""
    reason: str = ""
    endpoint: str = ""
    api_key: str = field(default="", repr=False)
    pool_session_key: str = field(default="", repr=False)
    pool_failure_reporter: PoolFailureReporter | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def public_payload(self) -> dict[str, object]:
        return {
            "providerId": self.provider_id,
            "available": self.available,
            "source": self.source,
            "owner": self.owner,
            "kind": self.kind,
            "envKey": self.env_key,
            "reason": self.reason,
        }


def _text(value: object) -> str:
    return str(value or "").strip()


def _field_was_set(config: object | None, name: str) -> bool:
    fields_set = getattr(config, "model_fields_set", None)
    return isinstance(fields_set, set) and name in fields_set


def _image_env_was_authored(
    *,
    gateway_config: object | None,
    provider_id: str,
    provider_config: object | None,
) -> bool:
    """Return whether the image env reference came from operator config.

    Loaded settings can materialize nested schema defaults into
    ``model_fields_set``. Their raw persistence snapshot is therefore the
    authoritative source; directly constructed configs and environment-backed
    settings fall back to ordinary Pydantic field provenance.
    """

    settings_env = (
        "OPENSQUILLA_IMAGE_GENERATION_PROVIDERS__"
        f"{provider_id.strip().upper().replace('-', '_')}__API_KEY_ENV"
    )
    if environment_value(settings_env):
        return True
    if gateway_config is None:
        # Legacy/provider-only callers do not carry persistence provenance.
        # Treat a schema-default name as implicit, preserving the historical
        # same-provider LLM fallback at that compatibility boundary.
        return False

    raw = getattr(gateway_config, "_persist_raw_base", None)
    if isinstance(raw, Mapping):
        image = raw.get("image_generation")
        providers = image.get("providers") if isinstance(image, Mapping) else None
        if isinstance(providers, Mapping):
            provider = provider_id.strip().lower()
            for key, candidate in providers.items():
                if str(key or "").strip().lower() != provider:
                    continue
                return isinstance(candidate, Mapping) and "api_key_env" in candidate
        return False
    return _field_was_set(provider_config, "api_key_env")


def _profile_for(config: object | None, provider_id: str) -> object | None:
    profiles = getattr(config, "llm_profiles", None) or {}
    if not isinstance(profiles, dict):
        return None
    provider = provider_id.strip().lower()
    for key, profile in profiles.items():
        if _text(key).lower() == provider:
            return cast(object, profile)
    return None


def _missing_model_service_resolution(
    *,
    config: object | None,
    provider_id: str,
    endpoint: str,
) -> ImageGenerationCredentialResolution:
    active = getattr(config, "llm", None)
    active_matches = _text(getattr(active, "provider", "")).lower() == provider_id
    deployment = active if active_matches else _profile_for(config, provider_id)
    owner: ImageCredentialOwner = (
        "primary" if active_matches else ("profile" if deployment is not None else "none")
    )
    if deployment is None:
        return ImageGenerationCredentialResolution(
            provider_id=provider_id,
            source="none",
            owner="none",
            kind="none",
            available=False,
            reason="model_service_not_configured",
            endpoint=endpoint,
        )

    pool_names = [
        _text(name)
        for name in (getattr(deployment, "api_key_env_pool", None) or [])
        if _text(name)
    ]
    env_key = _text(getattr(deployment, "api_key_env", ""))
    if pool_names:
        return ImageGenerationCredentialResolution(
            provider_id=provider_id,
            source="missing_env",
            owner=owner,
            kind="pool",
            available=False,
            env_key=pool_names[0],
            reason="credential_pool_unavailable",
            endpoint=endpoint,
        )
    if env_key:
        return ImageGenerationCredentialResolution(
            provider_id=provider_id,
            source="missing_env",
            owner=owner,
            kind="env",
            available=False,
            env_key=env_key,
            reason="model_service_env_missing",
            endpoint=endpoint,
        )
    return ImageGenerationCredentialResolution(
        provider_id=provider_id,
        source="none",
        owner=owner,
        kind="none",
        available=False,
        reason="model_service_credential_missing",
        endpoint=endpoint,
    )


def _model_service_resolution(
    *,
    gateway_config: object,
    provider_id: str,
    model: str,
    runtime: bool,
    session_key: str,
) -> tuple[object | None, dict[str, object]]:
    """Resolve the matching primary/profile through the shared deployment layer."""

    from opensquilla.provider.deployment import resolve_provider_deployment
    from opensquilla.provider.selector import ProviderConfig

    inherited: ProviderConfig | None = None
    active = getattr(gateway_config, "llm", None)
    active_provider = _text(getattr(active, "provider", "")).lower()
    if active_provider:
        try:
            scratch = cast(_ModelCopyConfig, gateway_config).model_copy(deep=True)
            runtime_resolver = getattr(
                scratch,
                "_resolve_image_generation_llm_runtime",
                None,
            )
            if callable(runtime_resolver):
                resolved = runtime_resolver()
                inherited = ProviderConfig(
                    provider=_text(getattr(resolved, "provider", "")),
                    model=model,
                    api_key=_text(getattr(resolved, "api_key", "")),
                    base_url=_text(getattr(resolved, "base_url", "")),
                    proxy=_text(getattr(resolved, "proxy", "")),
                    provider_routing=dict(
                        getattr(resolved, "provider_routing", {}) or {}
                    ),
                )
            else:
                inherited = ProviderConfig(
                    provider=active_provider,
                    model=model,
                    api_key=_text(getattr(active, "api_key", "")),
                    base_url=_text(getattr(active, "base_url", "")),
                    proxy=_text(getattr(active, "proxy", "")),
                )
        except (KeyError, TypeError, ValueError):
            inherited = ProviderConfig(
                provider=active_provider,
                model=model,
                api_key=_text(getattr(active, "api_key", "")),
                base_url=_text(getattr(active, "base_url", "")),
                proxy=_text(getattr(active, "proxy", "")),
            )

    turn_metadata: dict[str, object] = {}
    pool_acquirer = None
    if runtime:
        from opensquilla.provider.credentials import NoCredentialsAvailable
        from opensquilla.provider.deployment import CredentialPoolExhaustedError

        gateway_pool_acquirer = getattr(
            gateway_config,
            "_acquire_image_generation_profile_credential",
            None,
        )

        def acquire_pool_credential(
            requested_provider: str,
            env_pool: list[str],
            requested_session_key: str,
        ) -> object | None:
            if not callable(gateway_pool_acquirer):
                return None
            try:
                return cast(
                    object | None,
                    gateway_pool_acquirer(
                        requested_provider,
                        env_pool,
                        requested_session_key,
                    ),
                )
            except NoCredentialsAvailable as exc:
                raise CredentialPoolExhaustedError from exc
        if callable(gateway_pool_acquirer):
            pool_acquirer = acquire_pool_credential

    resolution = resolve_provider_deployment(
        gateway_config,
        provider_id,
        model or "image-generation",
        inherited_provider_config=inherited,
        session_key=session_key,
        turn_metadata=turn_metadata,
        credential_pool_acquirer=pool_acquirer,
    )
    return resolution, turn_metadata


def resolve_image_generation_credential(
    *,
    provider_id: str,
    provider_config: object | None,
    default_env_key: str,
    default_base_url: str,
    effective_base_url: str,
    gateway_config: object | None = None,
    llm_config: object | None = None,
    model: str = "image-generation",
    runtime: bool = False,
    session_key: str = "",
    include_image_credentials: bool = True,
) -> ImageGenerationCredentialResolution:
    """Resolve one provider without copying a model-service secret.

    A user-authored image env reference is authoritative: when it is missing,
    report ``missing_env`` instead of silently falling through to a different
    credential. Schema-default env names are hints only and may fall through.
    """

    provider = _text(provider_id).lower()
    endpoint = _text(effective_base_url) or _text(default_base_url)
    direct_key = _text(getattr(provider_config, "api_key", ""))
    if include_image_credentials and direct_key:
        return ImageGenerationCredentialResolution(
            provider_id=provider,
            source="explicit",
            owner="image",
            kind="direct",
            available=True,
            reason="ready",
            endpoint=endpoint,
            api_key=direct_key,
        )

    configured_env = _text(getattr(provider_config, "api_key_env", ""))
    env_explicit = _field_was_set(provider_config, "api_key_env")
    effective_env = credential_env_for_endpoint(
        configured_env=configured_env,
        configured_explicitly=env_explicit,
        default_env=default_env_key,
        default_base_url=default_base_url,
        effective_base_url=endpoint,
    )
    implicit_env_value = ""
    if include_image_credentials and effective_env:
        env_value = _text(environment_value(effective_env))
        authored_env = bool(
            configured_env
            and (
                configured_env != _text(default_env_key)
                or _image_env_was_authored(
                    gateway_config=gateway_config,
                    provider_id=provider,
                    provider_config=provider_config,
                )
            )
        )
        if env_value and authored_env:
            return ImageGenerationCredentialResolution(
                provider_id=provider,
                source="env",
                owner="image",
                kind="env",
                available=True,
                env_key=effective_env,
                reason="ready",
                endpoint=endpoint,
                api_key=env_value,
            )
        if authored_env:
            return ImageGenerationCredentialResolution(
                provider_id=provider,
                source="missing_env",
                owner="image",
                kind="env",
                available=False,
                env_key=effective_env,
                reason="image_env_missing",
                endpoint=endpoint,
            )
        implicit_env_value = env_value

    if gateway_config is not None:
        active = getattr(gateway_config, "llm", None)
        active_matches = _text(getattr(active, "provider", "")).lower() == provider
        profile = _profile_for(gateway_config, provider)
        if not active_matches and profile is None:
            if implicit_env_value:
                return ImageGenerationCredentialResolution(
                    provider_id=provider,
                    source="env",
                    owner="image",
                    kind="env",
                    available=True,
                    env_key=effective_env,
                    reason="ready",
                    endpoint=endpoint,
                    api_key=implicit_env_value,
                )
            return _missing_model_service_resolution(
                config=gateway_config,
                provider_id=provider,
                endpoint=endpoint,
            )
        resolution, metadata = _model_service_resolution(
            gateway_config=gateway_config,
            provider_id=provider,
            model=model,
            runtime=runtime,
            session_key=session_key,
        )
        provider_cfg = getattr(resolution, "provider_config", None)
        deployment_endpoint = _text(getattr(provider_cfg, "base_url", ""))
        api_key = _text(getattr(provider_cfg, "api_key", ""))
        if not getattr(resolution, "ready", False) or not api_key:
            return _missing_model_service_resolution(
                config=gateway_config,
                provider_id=provider,
                endpoint=deployment_endpoint or endpoint,
            )
        if not base_url_allows_credential_reuse(deployment_endpoint, endpoint):
            return ImageGenerationCredentialResolution(
                provider_id=provider,
                source="none",
                owner="primary" if active_matches else "profile",
                kind="none",
                available=False,
                reason="endpoint_mismatch",
                endpoint=endpoint,
            )
        credential_source = _text(getattr(resolution, "credential_source", ""))
        env_key = _text(getattr(resolution, "credential_env", ""))
        if active_matches and not _text(getattr(active, "api_key", "")):
            active_env = _text(getattr(active, "api_key_env", ""))
            if active_env and _text(environment_value(active_env)):
                env_key = active_env
            elif default_env_key and _text(environment_value(default_env_key)):
                env_key = default_env_key
        pool_info = metadata.get("credential_pool")
        is_pool = isinstance(pool_info, dict) or credential_source.startswith(
            "profile_pool"
        )
        kind: ImageCredentialKind = (
            "pool" if is_pool else ("env" if env_key else "direct")
        )
        pool_failure_reporter = getattr(
            gateway_config,
            "_report_image_generation_profile_credential_failure",
            None,
        )
        return ImageGenerationCredentialResolution(
            provider_id=provider,
            source="llm_fallback",
            owner="primary" if active_matches else "profile",
            kind=kind,
            available=True,
            env_key=env_key,
            reason="ready",
            endpoint=endpoint,
            api_key=api_key,
            pool_session_key=session_key if is_pool else "",
            pool_failure_reporter=(
                pool_failure_reporter if callable(pool_failure_reporter) else None
            ),
        )

    llm = llm_config
    if _text(getattr(llm, "provider", "")).lower() == provider:
        llm_endpoint = (
            _text(getattr(llm, "base_url", ""))
            if _field_was_set(llm, "base_url")
            else ""
        ) or default_base_url
        if not base_url_allows_credential_reuse(llm_endpoint, endpoint):
            return ImageGenerationCredentialResolution(
                provider_id=provider,
                source="none",
                owner="primary",
                kind="none",
                available=False,
                reason="endpoint_mismatch",
                endpoint=endpoint,
            )
        api_key = _text(getattr(llm, "api_key", ""))
        env_key = _text(getattr(llm, "api_key_env", ""))
        if api_key:
            return ImageGenerationCredentialResolution(
                provider_id=provider,
                source="llm_fallback",
                owner="primary",
                kind="direct",
                available=True,
                reason="ready",
                endpoint=endpoint,
                api_key=api_key,
            )
        if env_key and environment_value(env_key):
            return ImageGenerationCredentialResolution(
                provider_id=provider,
                source="llm_fallback",
                owner="primary",
                kind="env",
                available=True,
                env_key=env_key,
                reason="ready",
                endpoint=endpoint,
                api_key=_text(environment_value(env_key)),
            )
        if env_key:
            return ImageGenerationCredentialResolution(
                provider_id=provider,
                source="missing_env",
                owner="primary",
                kind="env",
                available=False,
                env_key=env_key,
                reason="model_service_env_missing",
                endpoint=endpoint,
            )

    if implicit_env_value:
        return ImageGenerationCredentialResolution(
            provider_id=provider,
            source="env",
            owner="image",
            kind="env",
            available=True,
            env_key=effective_env,
            reason="ready",
            endpoint=endpoint,
            api_key=implicit_env_value,
        )

    return ImageGenerationCredentialResolution(
        provider_id=provider,
        source="none",
        owner="none",
        kind="none",
        available=False,
        env_key=effective_env or _text(default_env_key),
        reason="credential_missing",
        endpoint=endpoint,
    )


def report_image_generation_pool_failure(
    resolution: ImageGenerationCredentialResolution | None,
    exc: BaseException,
) -> None:
    """Report attributable pooled-key failures without exposing the secret."""

    if (
        resolution is None
        or resolution.kind != "pool"
        or not resolution.pool_session_key
    ):
        return
    try:
        import httpx

        from opensquilla.provider.failures import (
            classify_provider_error,
            retry_after_from_headers,
        )

        status_code: int | None = None
        headers = None
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = int(exc.response.status_code)
            headers = exc.response.headers
        kind = classify_provider_error(
            provider_name=resolution.provider_id,
            status_code=status_code,
            message=str(exc),
        )
        reporter = resolution.pool_failure_reporter
        if reporter is None:
            return
        reporter(
            resolution.provider_id,
            resolution.pool_session_key,
            kind,
            (
                retry_after_from_headers(status_code, headers)
                if status_code is not None
                else None
            ),
        )
    except Exception:
        # Credential bookkeeping must never replace the provider failure.
        return


__all__ = [
    "ImageGenerationCredentialResolution",
    "report_image_generation_pool_failure",
    "resolve_image_generation_credential",
]
