"""Video credential resolution shared by runtime and setup status consumers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from opensquilla.endpoint_identity import (
    base_url_allows_credential_reuse,
    credential_env_for_endpoint,
)
from opensquilla.environment import environment_value
from opensquilla.provider.image_generation_credentials import (
    resolve_image_generation_credential,
)
from opensquilla.provider.image_generation_policy import (
    IMAGE_GENERATION_OFFICIAL_BASE_URLS,
    conflicting_image_generation_endpoint_provider,
    is_valid_image_generation_base_url,
    resolve_image_generation_base_url,
)
from opensquilla.provider.video_generation_policy import (
    conflicting_video_generation_endpoint_provider,
    is_valid_video_generation_base_url,
)
from opensquilla.video_generation_defaults import (
    VIDEO_GENERATION_DEFAULT_ENV_KEYS,
    VIDEO_GENERATION_OFFICIAL_BASE_URLS,
)


@dataclass(frozen=True)
class VideoGenerationCredential:
    available: bool
    api_key: str = field(default="", repr=False)
    env_key: str = ""
    source: str = "none"
    owner: str = "none"


def video_generation_provider(config: object | None) -> str:
    provider = str(getattr(config, "provider", "") or "").strip().lower()
    if not provider and str(getattr(config, "primary", "") or "").strip():
        return "openrouter"
    return provider


def _provider_config(config: object | None, provider: str) -> object | None:
    providers = getattr(config, "providers", None)
    return getattr(providers, provider, None) if providers is not None else None


def _setting_was_authored(
    section_name: str,
    provider: str,
    provider_config: object | None,
    gateway_config: object | None,
    field_name: str,
) -> bool:
    setting_name = (
        f"OPENSQUILLA_{section_name.upper()}_PROVIDERS__{provider.upper()}__"
        f"{field_name.upper()}"
    )
    if environment_value(setting_name):
        return True
    raw = getattr(gateway_config, "_persist_raw_base", None)
    if isinstance(raw, Mapping):
        section = raw.get(section_name)
        providers = section.get("providers") if isinstance(section, Mapping) else None
        if isinstance(providers, Mapping):
            for key, candidate in providers.items():
                if str(key).strip().lower() == provider:
                    return isinstance(candidate, Mapping) and field_name in candidate
        return False
    fields_set = getattr(provider_config, "model_fields_set", None)
    return isinstance(fields_set, set) and field_name in fields_set


def _env_was_authored(
    section_name: str,
    provider: str,
    provider_config: object | None,
    gateway_config: object | None,
) -> bool:
    configured_env = str(getattr(provider_config, "api_key_env", "") or "").strip()
    return bool(configured_env) and (
        configured_env != VIDEO_GENERATION_DEFAULT_ENV_KEYS.get(provider, "")
        or _setting_was_authored(
            section_name, provider, provider_config, gateway_config, "api_key_env"
        )
    )


def _model_service(
    gateway_config: object | None, provider: str
) -> tuple[object | None, str]:
    active = getattr(gateway_config, "llm", None)
    if str(getattr(active, "provider", "") or "").strip().lower() == provider:
        return active, "primary"
    profiles = getattr(gateway_config, "llm_profiles", None)
    if isinstance(profiles, Mapping):
        for key, candidate in profiles.items():
            if str(key).strip().lower() == provider:
                return candidate, "profile"
    return None, "none"


def _configured_model_service(gateway_config: object | None, provider: str) -> bool:
    deployment, owner = _model_service(gateway_config, provider)
    if deployment is None:
        return False
    if owner == "profile" or any(
        str(getattr(deployment, name, "") or "").strip()
        for name in ("api_key", "api_key_env")
    ):
        return True
    raw = getattr(gateway_config, "_persist_raw_base", None)
    if isinstance(raw, Mapping):
        return bool(raw.get("llm")) or any(
            environment_value(f"OPENSQUILLA_LLM_{name}")
            for name in ("PROVIDER", "MODEL", "BASE_URL", "API_KEY", "API_KEY_ENV")
        )
    fields_set = getattr(deployment, "model_fields_set", None)
    return isinstance(fields_set, set) and bool(
        fields_set & {"provider", "model", "base_url", "api_key", "api_key_env"}
    )


def _missing_model_service_reference(
    gateway_config: object | None, provider: str
) -> VideoGenerationCredential | None:
    deployment, owner = _model_service(gateway_config, provider)
    if deployment is None:
        return None
    key_path = "llm.api_key" if owner == "primary" else f"llm_profiles.{provider}.api_key"
    direct_key = str(getattr(deployment, "api_key", "") or "").strip()
    if direct_key and key_path not in getattr(gateway_config, "_runtime_secret_paths", ()):
        return None
    pool_names = [
        str(name).strip()
        for name in (getattr(deployment, "api_key_env_pool", None) or [])
        if str(name).strip()
    ]
    if pool_names:
        if any(environment_value(name).strip() for name in pool_names):
            return None
        return VideoGenerationCredential(
            available=False, env_key=pool_names[0], source="missing_env", owner=owner
        )
    env_key = str(getattr(deployment, "api_key_env", "") or "").strip()
    if owner == "primary" and not env_key:
        env_key = environment_value("OPENSQUILLA_LLM_API_KEY_ENV").strip()
    if env_key and not environment_value(env_key).strip():
        return VideoGenerationCredential(
            available=False, env_key=env_key, source="missing_env", owner=owner
        )
    return None


def _shared_base_url(gateway_config: object | None, provider: str) -> tuple[str, str]:
    deployment, owner = _model_service(gateway_config, provider)
    if deployment is None or not _configured_model_service(gateway_config, provider):
        return "", "none"
    endpoint = str(getattr(deployment, "base_url", "") or "").strip()
    if owner == "primary":
        copy_config = getattr(gateway_config, "model_copy", None)
        if callable(copy_config):
            try:
                scratch = copy_config(deep=True)
                resolve_runtime = getattr(scratch, "_resolve_image_generation_llm_runtime", None)
                if callable(resolve_runtime):
                    resolved = resolve_runtime()
                    endpoint = str(getattr(resolved, "base_url", "") or "").strip()
            except Exception:
                return "", "none"
    if not endpoint:
        endpoint = VIDEO_GENERATION_OFFICIAL_BASE_URLS.get(provider, "")
    if (
        not is_valid_video_generation_base_url(endpoint)
        or conflicting_video_generation_endpoint_provider(provider, endpoint) is not None
    ):
        return "", "none"
    return endpoint, owner


def _video_endpoint(
    config: object | None, provider: str, gateway_config: object | None
) -> tuple[str, str, bool]:
    default = VIDEO_GENERATION_OFFICIAL_BASE_URLS.get(provider, "")
    selected = _provider_config(config, provider)
    endpoint = str(getattr(selected, "base_url", default) or default).strip()
    authored = endpoint != default or _setting_was_authored(
        "video_generation", provider, selected, gateway_config, "base_url"
    )
    dedicated_credential = bool(str(getattr(selected, "api_key", "") or "").strip()) or (
        _env_was_authored("video_generation", provider, selected, gateway_config)
    )
    if provider in {"tokenrhythm", "openrouter"} and not authored and not dedicated_credential:
        shared_endpoint, owner = _shared_base_url(gateway_config, provider)
        if shared_endpoint:
            return shared_endpoint, owner, False
    return endpoint, "video" if authored or dedicated_credential else "default", authored


def video_generation_base_url(
    config: object | None, provider: str, *, gateway_config: object | None = None
) -> str:
    """Resolve the request endpoint without moving dedicated video credentials."""
    return _video_endpoint(config, provider, gateway_config)[0]


def _image_credential(
    *, provider: str, endpoint: str, gateway_config: object | None
) -> VideoGenerationCredential | None:
    if gateway_config is None or provider not in IMAGE_GENERATION_OFFICIAL_BASE_URLS:
        return None
    image_provider = _provider_config(getattr(gateway_config, "image_generation", None), provider)
    if image_provider is None:
        return None
    default_endpoint = IMAGE_GENERATION_OFFICIAL_BASE_URLS[provider]
    image_endpoint = resolve_image_generation_base_url(
        provider_id=provider,
        provider_config=image_provider,
        llm_config=getattr(gateway_config, "llm", None),
        default_base_url=default_endpoint,
        gateway_config=gateway_config,
    )
    if (
        not is_valid_image_generation_base_url(image_endpoint)
        or conflicting_image_generation_endpoint_provider(provider, image_endpoint) is not None
        or not base_url_allows_credential_reuse(image_endpoint, endpoint)
    ):
        return None
    direct_key = str(getattr(image_provider, "api_key", "") or "").strip()
    if direct_key:
        return VideoGenerationCredential(
            available=True, api_key=direct_key, source="image_direct", owner="image"
        )
    default_env = VIDEO_GENERATION_DEFAULT_ENV_KEYS[provider]
    configured_env = str(getattr(image_provider, "api_key_env", default_env) or "").strip()
    authored_env = _env_was_authored("image_generation", provider, image_provider, gateway_config)
    env_key = credential_env_for_endpoint(
        configured_env=configured_env,
        configured_explicitly=authored_env,
        default_env=default_env,
        default_base_url=default_endpoint,
        effective_base_url=image_endpoint,
    )
    if not env_key:
        return None
    image_key = environment_value(env_key).strip()
    if image_key:
        return VideoGenerationCredential(
            available=True,
            api_key=image_key,
            env_key=env_key,
            source="image_env",
            owner="image",
        )
    if authored_env:
        return VideoGenerationCredential(
            available=False, env_key=env_key, source="missing_env", owner="image"
        )
    return None


def resolve_video_generation_credential(
    config: object | None,
    *,
    provider_id: str | None = None,
    base_url: str | None = None,
    gateway_config: object | None = None,
    runtime: bool = False,
    session_key: str = "",
) -> VideoGenerationCredential:
    """Resolve a route using explicit configuration and optional runtime identity."""

    selected = provider_id or video_generation_provider(config)
    default_env = VIDEO_GENERATION_DEFAULT_ENV_KEYS.get(selected)
    default_endpoint = VIDEO_GENERATION_OFFICIAL_BASE_URLS.get(selected)
    if not default_env or not default_endpoint:
        return VideoGenerationCredential(available=False)
    endpoint = base_url or video_generation_base_url(
        config, selected, gateway_config=gateway_config
    )
    if (
        not is_valid_video_generation_base_url(endpoint)
        or conflicting_video_generation_endpoint_provider(selected, endpoint) is not None
    ):
        return VideoGenerationCredential(available=False)
    provider_config = _provider_config(config, selected)
    direct_key = str(getattr(provider_config, "api_key", "") or "").strip()
    stale_direct_key = False
    if direct_key:
        bound_endpoint = str(getattr(provider_config, "api_key_base_url", "") or "").strip()
        if not bound_endpoint or not base_url_allows_credential_reuse(bound_endpoint, endpoint):
            stale_direct_key = True
        else:
            direct_key_path = f"video_generation.providers.{selected}.api_key"
            from_environment = direct_key_path in getattr(
                gateway_config, "_runtime_secret_paths", ()
            )
            return VideoGenerationCredential(
                available=True,
                api_key=direct_key,
                env_key=(
                    f"OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__{selected.upper()}__API_KEY"
                    if from_environment
                    else ""
                ),
                source="video_env_injected_direct" if from_environment else "video_direct",
                owner="video",
            )
    configured_env = str(getattr(provider_config, "api_key_env", default_env) or "").strip()
    authored_env = _env_was_authored("video_generation", selected, provider_config, gateway_config)
    if stale_direct_key and not authored_env:
        return VideoGenerationCredential(available=False, owner="video")
    env_key = credential_env_for_endpoint(
        configured_env=configured_env,
        configured_explicitly=authored_env,
        default_env=default_env,
        default_base_url=default_endpoint,
        effective_base_url=endpoint,
    )
    if env_key and authored_env:
        env_value = environment_value(env_key).strip()
        if env_value:
            return VideoGenerationCredential(
                available=True,
                api_key=env_value,
                env_key=env_key,
                source="video_env",
                owner="video",
            )
        return VideoGenerationCredential(
            available=False, env_key=env_key, source="missing_env", owner="video"
        )
    if stale_direct_key:
        return VideoGenerationCredential(available=False, owner="video")
    missing_reference = _missing_model_service_reference(gateway_config, selected)
    if missing_reference is not None:
        return missing_reference
    resolution = resolve_image_generation_credential(
        provider_id=selected,
        provider_config=None,
        default_env_key="",
        default_base_url=default_endpoint,
        effective_base_url=endpoint,
        gateway_config=gateway_config,
        model=str(getattr(config, "primary", "") or ""),
        runtime=runtime,
        session_key=session_key,
        include_image_credentials=False,
    )
    shared_credential = VideoGenerationCredential(
        available=resolution.available,
        api_key=resolution.api_key,
        env_key=resolution.env_key,
        source="llm_fallback" if resolution.available else resolution.source,
        owner=resolution.owner,
    )
    if _configured_model_service(gateway_config, selected) or (
        shared_credential.available and shared_credential.owner != "primary"
    ):
        return shared_credential
    if env_key:
        env_value = environment_value(env_key).strip()
        if env_value:
            return VideoGenerationCredential(
                available=True,
                api_key=env_value,
                env_key=env_key,
                source="video_env",
                owner="video",
            )
    image_credential = _image_credential(
        provider=selected, endpoint=endpoint, gateway_config=gateway_config
    )
    return image_credential if image_credential is not None else shared_credential


def video_generation_credential_status(
    gateway_config: object, *, provider_id: str, base_url: str | None = None
) -> dict[str, object]:
    """Return credential availability and source for configuration surfaces."""

    provider = str(provider_id or "").strip().lower()
    video_config = getattr(gateway_config, "video_generation", None)
    provider_config = _provider_config(video_config, provider)
    endpoint, endpoint_source, endpoint_authored = _video_endpoint(
        video_config, provider, gateway_config
    )
    if base_url is not None:
        endpoint = base_url.strip()
    shared_endpoint, _ = _shared_base_url(gateway_config, provider)
    shared_available = False
    credential = VideoGenerationCredential(available=False)
    if provider in VIDEO_GENERATION_OFFICIAL_BASE_URLS and (base_url is None or base_url.strip()):
        try:
            credential = resolve_video_generation_credential(
                video_config,
                provider_id=provider,
                base_url=base_url,
                gateway_config=gateway_config,
                runtime=False,
            )
        except Exception:
            credential = VideoGenerationCredential(available=False)
        if shared_endpoint and _missing_model_service_reference(gateway_config, provider) is None:
            try:
                shared = resolve_image_generation_credential(
                    provider_id=provider,
                    provider_config=None,
                    default_env_key="",
                    default_base_url=VIDEO_GENERATION_OFFICIAL_BASE_URLS[provider],
                    effective_base_url=shared_endpoint,
                    gateway_config=gateway_config,
                    model=str(getattr(video_config, "primary", "") or "video-generation"),
                    runtime=False,
                    include_image_credentials=False,
                )
                shared_available = shared.available
            except Exception:
                shared_available = False
    return {
        "providerId": provider,
        "available": credential.available,
        "source": credential.source,
        "owner": credential.owner,
        "envKey": credential.env_key,
        "clearable": credential.available and credential.source == "video_direct",
        "baseUrl": endpoint if is_valid_video_generation_base_url(endpoint) else "",
        "baseUrlSource": endpoint_source,
        "baseUrlAuthored": endpoint_authored,
        "apiKeyEnvAuthored": _env_was_authored(
            "video_generation", provider, provider_config, gateway_config
        ),
        "sharedBaseUrl": shared_endpoint,
        "sharedCredentialAvailable": shared_available,
    }


__all__ = [
    "VideoGenerationCredential",
    "resolve_video_generation_credential",
    "video_generation_base_url",
    "video_generation_credential_status",
    "video_generation_provider",
]
