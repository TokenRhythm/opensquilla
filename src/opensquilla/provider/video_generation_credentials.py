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


def video_generation_base_url(config: object | None, provider: str) -> str:
    default = VIDEO_GENERATION_OFFICIAL_BASE_URLS.get(provider, "")
    selected = _provider_config(config, provider)
    return str(getattr(selected, "base_url", default) or default).strip()


def _env_was_authored(
    section_name: str,
    provider: str,
    provider_config: object | None,
    gateway_config: object | None,
) -> bool:
    setting_name = f"OPENSQUILLA_{section_name.upper()}_PROVIDERS__{provider.upper()}__API_KEY_ENV"
    if environment_value(setting_name):
        return True
    raw = getattr(gateway_config, "_persist_raw_base", None)
    if isinstance(raw, Mapping):
        section = raw.get(section_name)
        providers = section.get("providers") if isinstance(section, Mapping) else None
        candidate = providers.get(provider) if isinstance(providers, Mapping) else None
        return isinstance(candidate, Mapping) and "api_key_env" in candidate
    fields_set = getattr(provider_config, "model_fields_set", None)
    return isinstance(fields_set, set) and "api_key_env" in fields_set


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
    endpoint = base_url or video_generation_base_url(config, selected)
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
    if env_key:
        direct_key = environment_value(env_key).strip()
        if direct_key:
            return VideoGenerationCredential(
                available=True,
                api_key=direct_key,
                env_key=env_key,
                source="video_env",
                owner="video",
            )
        if authored_env:
            return VideoGenerationCredential(
                available=False, env_key=env_key, source="missing_env", owner="video"
            )
    if stale_direct_key:
        return VideoGenerationCredential(available=False, owner="video")
    image_credential = _image_credential(
        provider=selected, endpoint=endpoint, gateway_config=gateway_config
    )
    if image_credential is not None:
        return image_credential
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
    return VideoGenerationCredential(
        available=resolution.available,
        api_key=resolution.api_key,
        env_key=resolution.env_key,
        source="llm_fallback" if resolution.available else resolution.source,
        owner=resolution.owner,
    )


def video_generation_credential_status(
    gateway_config: object, *, provider_id: str, base_url: str | None = None
) -> dict[str, object]:
    """Return credential availability and source for configuration surfaces."""

    provider = str(provider_id or "").strip().lower()
    credential = VideoGenerationCredential(available=False)
    if provider in VIDEO_GENERATION_OFFICIAL_BASE_URLS and (base_url is None or base_url.strip()):
        try:
            credential = resolve_video_generation_credential(
                getattr(gateway_config, "video_generation", None),
                provider_id=provider,
                base_url=base_url,
                gateway_config=gateway_config,
                runtime=False,
            )
        except Exception:
            credential = VideoGenerationCredential(available=False)
    return {
        "providerId": provider,
        "available": credential.available,
        "source": credential.source,
        "owner": credential.owner,
        "envKey": credential.env_key,
        "clearable": credential.available and credential.source == "video_direct",
    }


__all__ = [
    "VideoGenerationCredential",
    "resolve_video_generation_credential",
    "video_generation_base_url",
    "video_generation_credential_status",
    "video_generation_provider",
]
