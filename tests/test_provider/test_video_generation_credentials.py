"""Video credentials resolve from supplied state without a tool context."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import tomli_w

from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.config_store import load_config
from opensquilla.onboarding.video_generation_state import resolve_video_generation_state
from opensquilla.provider import video_generation_credentials as credentials

_MODELS = {"tokenrhythm": "wan3.0-video", "openrouter": "google/veo-3.1-fast"}


@pytest.fixture(autouse=True)
def isolate_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for provider in ("TOKENRHYTHM", "OPENROUTER"):
        for suffix in ("API_KEY", "BASE_URL"):
            monkeypatch.delenv(f"{provider}_{suffix}", raising=False)
            monkeypatch.delenv(
                f"OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__{provider}__{suffix}", raising=False
            )
        monkeypatch.delenv(
            f"OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__{provider}__API_KEY_ENV", raising=False
        )
    for suffix in ("PROVIDER", "MODEL", "API_KEY", "API_KEY_ENV", "BASE_URL"):
        monkeypatch.delenv(f"OPENSQUILLA_LLM_{suffix}", raising=False)


def _gateway(tmp_path: Path, payload: dict[str, Any]) -> GatewayConfig:
    config_path = tmp_path / "video-config.toml"
    config_path.write_text(tomli_w.dumps(payload), encoding="utf-8")
    return load_config(config_path)


def _video(provider: str) -> dict[str, Any]:
    return {"enabled": True, "provider": provider, "primary": _MODELS[provider]}


def _resolve(gateway: GatewayConfig, **kwargs: Any) -> credentials.VideoGenerationCredential:
    return credentials.resolve_video_generation_credential(
        gateway.video_generation, gateway_config=gateway, **kwargs
    )


def test_runtime_fallback_receives_explicit_session_and_hides_the_key(monkeypatch) -> None:
    calls = []

    def resolve_image_credential(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            available=True,
            api_key="synthetic-runtime-video-key",
            env_key="DUMMY_VIDEO_PROFILE_KEY",
            source="profile_pool",
            owner="profile",
        )

    monkeypatch.setattr(credentials, "environment_value", lambda _: "")
    monkeypatch.setattr(
        credentials, "resolve_image_generation_credential", resolve_image_credential
    )
    config = SimpleNamespace(provider="xai", primary="grok-imagine-video-1.5")
    gateway = SimpleNamespace()

    result = credentials.resolve_video_generation_credential(
        config,
        gateway_config=gateway,
        runtime=True,
        session_key="synthetic-video-session",
    )

    assert result.available is True
    assert result.source == "llm_fallback"
    assert result.api_key == "synthetic-runtime-video-key"
    assert "synthetic-runtime-video-key" not in repr(result)
    assert calls[0]["runtime"] is True
    assert calls[0]["session_key"] == "synthetic-video-session"
    assert calls[0]["gateway_config"] is gateway
    assert calls[0]["include_image_credentials"] is False


def test_status_fallback_does_not_acquire_a_runtime_credential(monkeypatch) -> None:
    calls = []

    def resolve_image_credential(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            available=False, api_key="", env_key="", source="none", owner="none"
        )

    monkeypatch.setattr(credentials, "environment_value", lambda _: "")
    monkeypatch.setattr(
        credentials, "resolve_image_generation_credential", resolve_image_credential
    )
    gateway = SimpleNamespace(video_generation=SimpleNamespace(provider="xai", primary="video"))

    status = credentials.video_generation_credential_status(gateway, provider_id="xai")

    assert status["available"] is False
    assert calls[0]["runtime"] is False
    assert calls[0]["session_key"] == ""


@pytest.mark.parametrize("provider", ["tokenrhythm", "openrouter"])
@pytest.mark.parametrize("owner", ["primary", "profile"])
@pytest.mark.parametrize("kind", ["direct", "env"])
def test_saved_provider_credentials_precede_default_environment_and_image_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, owner: str, kind: str
) -> None:
    endpoint = f"https://{provider}-proxy.example/v1"
    deployment = {"base_url": endpoint}
    if kind == "direct":
        deployment["api_key"] = "synthetic-shared-key"
    else:
        deployment["api_key_env"] = "SYNTHETIC_SHARED_KEY"
        monkeypatch.setenv("SYNTHETIC_SHARED_KEY", "synthetic-shared-key")
    payload: dict[str, Any] = {
        "video_generation": _video(provider),
        "image_generation": {
            "providers": {provider: {"api_key": "synthetic-image-key", "base_url": endpoint}}
        },
    }
    if owner == "primary":
        payload["llm"] = {"provider": provider, **deployment}
    else:
        payload["llm"] = {"provider": "openai", "api_key": "synthetic-other-provider-key"}
        payload["llm_profiles"] = {provider: deployment}
    monkeypatch.setenv(f"{provider.upper()}_API_KEY", "synthetic-ambient-key")
    gateway = _gateway(tmp_path, payload)

    result = _resolve(gateway, runtime=True, session_key="synthetic-video-session")
    status = credentials.video_generation_credential_status(gateway, provider_id=provider)
    state = resolve_video_generation_state(gateway)
    state_option = next(row for row in state["credentialOptions"] if row["providerId"] == provider)

    assert result.api_key == "synthetic-shared-key"
    assert result.source == "llm_fallback"
    assert result.owner == owner
    assert status == state_option
    assert status["available"] is True
    assert status["baseUrl"] == endpoint
    assert status["baseUrlSource"] == owner
    assert status["baseUrlAuthored"] is False
    assert status["apiKeyEnvAuthored"] is False
    assert status["sharedBaseUrl"] == endpoint
    assert status["sharedCredentialAvailable"] is True
    assert credentials.video_generation_base_url(
        gateway.video_generation, provider, gateway_config=gateway
    ) == endpoint
    public = json.dumps({"state": state, "config": gateway.to_public_dict()})
    assert "synthetic-shared-key" not in public
    assert "synthetic-image-key" not in public
    assert "synthetic-ambient-key" not in repr(result)
    assert gateway.video_generation.providers.model_dump()[provider]["api_key"] == ""


def test_profile_pool_status_is_read_only_and_runtime_uses_shared_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNTHETIC_VIDEO_POOL_A", "synthetic-pool-a-key")
    monkeypatch.setenv("SYNTHETIC_VIDEO_POOL_B", "synthetic-pool-b-key")
    calls = []

    def acquire(_config, provider, env_pool, session_key):
        calls.append((provider, env_pool, session_key))
        return SimpleNamespace(
            api_key="synthetic-pool-b-key", env_name=env_pool[1], key_id="synthetic-key-id"
        )

    monkeypatch.setattr(GatewayConfig, "_acquire_image_generation_profile_credential", acquire)
    gateway = _gateway(tmp_path, {
        "llm": {"provider": "openai", "api_key": "synthetic-other-provider-key"},
        "llm_profiles": {"openrouter": {
            "base_url": "https://pooled-video.example/v1",
            "api_key_env_pool": ["SYNTHETIC_VIDEO_POOL_A", "SYNTHETIC_VIDEO_POOL_B"],
        }},
        "video_generation": _video("openrouter"),
    })

    status = credentials.video_generation_credential_status(gateway, provider_id="openrouter")
    assert status["available"] is True
    assert status["envKey"] == "SYNTHETIC_VIDEO_POOL_A"
    assert status["baseUrl"] == "https://pooled-video.example/v1"
    assert calls == []
    result = _resolve(gateway, runtime=True, session_key="synthetic-pooled-session")
    assert result.api_key == "synthetic-pool-b-key"
    assert result.env_key == "SYNTHETIC_VIDEO_POOL_B"
    assert calls == [("openrouter", ["SYNTHETIC_VIDEO_POOL_A", "SYNTHETIC_VIDEO_POOL_B"],
                      "synthetic-pooled-session")]
    assert "synthetic-pool" not in json.dumps(status)


@pytest.mark.parametrize("kind", ["direct", "env", "missing_env"])
def test_dedicated_video_credentials_keep_video_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    dedicated = {"api_key": "synthetic-video-key"} if kind == "direct" else {
        "api_key_env": "SYNTHETIC_DEDICATED_VIDEO_KEY"
    }
    if kind == "env":
        monkeypatch.setenv("SYNTHETIC_DEDICATED_VIDEO_KEY", "synthetic-video-key")
    else:
        monkeypatch.delenv("SYNTHETIC_DEDICATED_VIDEO_KEY", raising=False)
    gateway = _gateway(tmp_path, {
        "llm": {"provider": "openrouter", "api_key": "synthetic-provider-key",
                "base_url": "https://provider-proxy.example/v1"},
        "video_generation": {**_video("openrouter"), "providers": {"openrouter": dedicated}},
    })

    result = _resolve(gateway)
    status = credentials.video_generation_credential_status(gateway, provider_id="openrouter")
    assert status["baseUrl"] == credentials.VIDEO_GENERATION_OFFICIAL_BASE_URLS["openrouter"]
    assert status["baseUrlSource"] == "video"
    assert status["baseUrlAuthored"] is False
    assert status["apiKeyEnvAuthored"] is (kind != "direct")
    assert status["sharedBaseUrl"] == "https://provider-proxy.example/v1"
    assert status["sharedCredentialAvailable"] is True
    assert result.available is (kind != "missing_env")
    assert result.api_key == ("" if kind == "missing_env" else "synthetic-video-key")
    assert result.source == {
        "direct": "video_direct", "env": "video_env", "missing_env": "missing_env"
    }[kind]


@pytest.mark.parametrize("endpoint", [
    credentials.VIDEO_GENERATION_OFFICIAL_BASE_URLS["openrouter"],
    "http://localhost/v1",
    "https://provider-proxy.example:8443/v1",
    "https://different-proxy.example/v1",
])
def test_authored_endpoint_cannot_borrow_provider_or_ambient_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-ambient-key")
    shared_endpoint = (
        "https://localhost/v1" if endpoint.startswith("http://")
        else "https://provider-proxy.example/v1"
    )
    gateway = _gateway(tmp_path, {
        "llm": {"provider": "openrouter", "api_key": "synthetic-provider-key",
                "base_url": shared_endpoint},
        "image_generation": {"providers": {"openrouter": {"api_key": "synthetic-image-key"}}},
        "video_generation": {**_video("openrouter"), "providers": {
            "openrouter": {"base_url": endpoint}
        }},
    })
    result = _resolve(gateway)
    status = credentials.video_generation_credential_status(gateway, provider_id="openrouter")
    assert result.available is False
    assert result.api_key == ""
    assert status["available"] is False
    assert status["baseUrl"] == endpoint
    assert status["baseUrlAuthored"] is True
    assert status["sharedCredentialAvailable"] is True


def test_materialized_defaults_do_not_pin_video_endpoint_or_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-ambient-key")
    gateway = _gateway(tmp_path, {
        "llm": {"provider": "openrouter", "api_key": "synthetic-provider-key",
                "base_url": "https://provider-proxy.example/v1"},
        "video_generation": _video("openrouter"),
    })
    provider_config = gateway.video_generation.providers.openrouter
    object.__setattr__(provider_config, "__pydantic_fields_set__", {"base_url", "api_key_env"})
    status = credentials.video_generation_credential_status(gateway, provider_id="openrouter")
    assert status["baseUrl"] == "https://provider-proxy.example/v1"
    assert status["baseUrlAuthored"] is False
    assert status["apiKeyEnvAuthored"] is False
    assert _resolve(gateway).api_key == "synthetic-provider-key"


def test_provider_base_url_environment_is_shared_without_mutating_provider_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://provider-env-proxy.example/v1")
    gateway = _gateway(tmp_path, {
        "llm": {"provider": "openrouter", "api_key": "synthetic-provider-key"},
        "video_generation": _video("openrouter"),
    })
    before = gateway.llm.model_dump()
    status = credentials.video_generation_credential_status(gateway, provider_id="openrouter")
    assert status["baseUrl"] == "https://provider-env-proxy.example/v1"
    assert status["available"] is True
    assert _resolve(gateway).api_key == "synthetic-provider-key"
    assert gateway.llm.model_dump() == before


def test_video_endpoint_environment_is_an_explicit_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__OPENROUTER__BASE_URL",
                       credentials.VIDEO_GENERATION_OFFICIAL_BASE_URLS["openrouter"])
    gateway = _gateway(tmp_path, {
        "llm": {"provider": "openrouter", "api_key": "synthetic-provider-key",
                "base_url": "https://provider-proxy.example/v1"},
        "video_generation": _video("openrouter"),
    })
    status = credentials.video_generation_credential_status(gateway, provider_id="openrouter")
    assert status["baseUrlAuthored"] is True
    assert status["baseUrl"] == credentials.VIDEO_GENERATION_OFFICIAL_BASE_URLS["openrouter"]
    assert status["available"] is False


def test_configured_provider_missing_key_does_not_fall_back_to_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SYNTHETIC_MISSING_PROVIDER_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-ambient-key")
    gateway = _gateway(tmp_path, {
        "llm": {"provider": "openrouter", "api_key_env": "SYNTHETIC_MISSING_PROVIDER_KEY"},
        "image_generation": {"providers": {"openrouter": {"api_key": "synthetic-image-key"}}},
        "video_generation": _video("openrouter"),
    })
    result = _resolve(gateway)
    assert result.available is False
    assert result.source == "missing_env"
    assert result.env_key == "SYNTHETIC_MISSING_PROVIDER_KEY"
    assert result.owner == "primary"
    status = credentials.video_generation_credential_status(gateway, provider_id="openrouter")
    assert status["available"] is False
    assert status["sharedCredentialAvailable"] is False


@pytest.mark.parametrize("kind", ["env", "pool"])
def test_missing_profile_reference_does_not_substitute_registry_or_image_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.delenv("SYNTHETIC_MISSING_PROFILE_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-ambient-key")
    profile = {"api_key_env": "SYNTHETIC_MISSING_PROFILE_KEY"} if kind == "env" else {
        "api_key_env_pool": ["SYNTHETIC_MISSING_PROFILE_KEY"]
    }
    gateway = _gateway(tmp_path, {
        "llm": {"provider": "openai", "api_key": "synthetic-other-provider-key"},
        "llm_profiles": {"openrouter": profile},
        "image_generation": {"providers": {"openrouter": {"api_key": "synthetic-image-key"}}},
        "video_generation": _video("openrouter"),
    })
    status = credentials.video_generation_credential_status(gateway, provider_id="openrouter")
    assert status["available"] is False
    assert status["sharedCredentialAvailable"] is False
    result = _resolve(gateway, runtime=True, session_key="synthetic-missing-profile-session")
    assert result.available is False
    assert result.source == "missing_env"
    assert result.env_key == "SYNTHETIC_MISSING_PROFILE_KEY"
    assert result.owner == "profile"


def test_unavailable_provider_resolver_does_not_break_saved_video_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _gateway(tmp_path, {
        "llm": {"provider": "openrouter", "api_key": "synthetic-provider-key"},
        "video_generation": {**_video("openrouter"), "providers": {
            "openrouter": {"api_key": "synthetic-video-key"}
        }},
    })

    def fail_resolution(_config):
        raise ValueError("synthetic unavailable provider")

    monkeypatch.setattr(GatewayConfig, "_resolve_image_generation_llm_runtime", fail_resolution)
    status = credentials.video_generation_credential_status(gateway, provider_id="openrouter")
    assert status["available"] is True
    assert status["source"] == "video_direct"
    assert status["sharedBaseUrl"] == ""
    assert status["sharedCredentialAvailable"] is False
    assert resolve_video_generation_state(gateway)["enabled"] is True
