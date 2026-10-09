from __future__ import annotations

import tomllib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from opensquilla.gateway.config import GatewayConfig, VideoGenerationConfig
from opensquilla.gateway.rpc_config import _handle_config_patch
from opensquilla.gateway.setup_config_runtime import sync_media_runtime
from opensquilla.provider.video_generation_policy import (
    VIDEO_GENERATION_DEFAULT_ENV_KEYS,
    VIDEO_GENERATION_OFFICIAL_BASE_URLS,
)


def test_video_generation_defaults_to_disabled_without_a_model() -> None:
    config = GatewayConfig()

    assert config.video_generation.enabled is False
    assert config.video_generation.provider == ""
    assert config.video_generation.effective_provider == ""
    assert config.video_generation.primary == ""
    assert config.video_generation.duration_seconds is None
    assert config.video_generation.max_duration_seconds == 8
    assert config.video_generation.aspect_ratio == "16:9"
    assert config.video_generation.allowed_aspect_ratios == ["16:9", "9:16"]
    assert config.video_generation.resolution == "720p"
    assert config.video_generation.allowed_resolutions == ["720p", "1080p"]
    for provider_id, official_url in VIDEO_GENERATION_OFFICIAL_BASE_URLS.items():
        provider = getattr(config.video_generation.providers, provider_id)
        assert provider.base_url == official_url
        assert provider.api_key == ""
        assert provider.api_key_base_url == ""
        assert provider.api_key_env == VIDEO_GENERATION_DEFAULT_ENV_KEYS[provider_id]


def test_video_generation_keeps_legacy_openrouter_model_when_provider_is_omitted() -> None:
    config = VideoGenerationConfig(enabled=True, primary="  google/veo-3.1-fast  ")

    assert config.provider == ""
    assert config.effective_provider == "openrouter"
    assert config.primary == "google/veo-3.1-fast"


def test_video_generation_requires_a_provider_native_model_when_enabled() -> None:
    with pytest.raises(ValidationError, match="provider and primary"):
        VideoGenerationConfig(enabled=True)

    for primary in ("veo-3.1-fast", "google/", "google/veo 3.1-fast", "google/../veo"):
        with pytest.raises(ValidationError, match="raw OpenRouter model ID"):
            VideoGenerationConfig(enabled=True, provider="openrouter", primary=primary)

    for primary in ("", "google/veo-3.1-fast", "veo 3.1", "veo?preview"):
        with pytest.raises(ValidationError, match="Gemini model ID"):
            VideoGenerationConfig(enabled=True, provider="gemini", primary=primary)

    gemini = VideoGenerationConfig(
        enabled=True, provider="gemini", primary=" veo-3.1-generate-preview "
    )
    assert gemini.provider == "gemini"
    assert gemini.effective_provider == "gemini"
    assert gemini.primary == "veo-3.1-generate-preview"


@pytest.mark.parametrize("provider", ["openai", "google", "unknown"])
def test_video_generation_does_not_accept_unimplemented_provider(provider: str) -> None:
    with pytest.raises(ValidationError, match="provider"):
        VideoGenerationConfig(provider=provider, enabled=False)


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("xai", "grok-imagine-video-1.5"),
        ("qwen", "wan2.7-t2v"),
        ("tokenrhythm", "wan3.0-video"),
        ("qwen_token_plan", "happyhorse-1.1-t2v"),
    ],
)
def test_video_generation_accepts_other_provider_native_models(provider: str, model: str) -> None:
    config = VideoGenerationConfig(enabled=True, provider=provider, primary=model)

    assert config.effective_provider == provider
    assert config.primary == model


@pytest.mark.parametrize("provider", ["xai", "qwen", "tokenrhythm", "qwen_token_plan"])
def test_video_generation_rejects_prefixed_provider_model_id(provider: str) -> None:
    with pytest.raises(ValidationError, match="raw provider video model ID"):
        VideoGenerationConfig(
            enabled=True,
            provider=provider,
            primary=f"{provider}/wan3.0-video",
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.com/v1",
        "https://example.com/v1?token=abc",
        "https://user:pass@example.com/v1",
        "https://example.com/v1/../private",
        "https://example.com/v1/%2e%2e/private",
        "https://example.com/v1#fragment",
        "https://example.com/v1 ",
        "file:///tmp/video",
    ],
)
def test_video_generation_rejects_unsafe_custom_base_url(base_url: str) -> None:
    with pytest.raises(ValidationError, match="base_url"):
        VideoGenerationConfig(providers={"openrouter": {"base_url": base_url}})


def test_video_generation_accepts_custom_https_and_loopback_endpoints() -> None:
    config = VideoGenerationConfig(
        providers={
            "openrouter": {"base_url": "https://media-gateway.example/api/v1"},
            "gemini": {"base_url": "http://127.0.0.1:9184/google/v1beta"},
        }
    )

    assert config.providers.openrouter.base_url == "https://media-gateway.example/api/v1"
    assert config.providers.gemini.base_url == "http://127.0.0.1:9184/google/v1beta"
    assert config.providers.openrouter.model_fields_set == {"base_url"}


def test_video_generation_rejects_cross_provider_official_origin() -> None:
    with pytest.raises(ValidationError, match="openrouter.*gemini"):
        VideoGenerationConfig(
            providers={"openrouter": {"base_url": "https://generativelanguage.googleapis.com/v1"}}
        )


def test_video_generation_validates_explicit_env_reference() -> None:
    with pytest.raises(ValidationError, match="api_key_env"):
        VideoGenerationConfig(providers={"xai": {"api_key_env": "BAD KEY"}})

    config = VideoGenerationConfig(providers={"xai": {"api_key_env": "CUSTOM_XAI_KEY"}})
    assert config.providers.xai.api_key_env == "CUSTOM_XAI_KEY"


def test_video_direct_key_is_bound_and_hidden_from_public_settings() -> None:
    gateway = GatewayConfig.model_validate(
        {
            "video_generation": {
                "provider": "tokenrhythm",
                "primary": "wan3.0-video",
                "providers": {
                    "tokenrhythm": {
                        "api_key": "synthetic-video-key",
                        "base_url": "https://video-proxy.example/v1",
                    }
                },
            }
        }
    )

    provider = gateway.video_generation.providers.tokenrhythm
    assert provider.api_key_base_url == "https://video-proxy.example/v1"
    stored = gateway.to_toml_dict()["video_generation"]["providers"]["tokenrhythm"]
    assert stored["api_key"] == "synthetic-video-key"
    assert stored["api_key_base_url"] == "https://video-proxy.example/v1"
    public = gateway.to_public_dict()["video_generation"]["providers"]["tokenrhythm"]
    assert public["api_key"] == "[redacted]"
    assert "api_key_base_url" not in public
    from opensquilla.tools.builtin import media

    assert media.video_generation_credential_status(
        gateway, provider_id="tokenrhythm"
    )["clearable"] is True


def test_environment_supplied_video_direct_key_is_not_persisted(tmp_path, monkeypatch) -> None:
    from opensquilla.onboarding.config_store import load_config, persist_config

    monkeypatch.setenv(
        "OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__TOKENRHYTHM__API_KEY",
        "synthetic-env-only-key",
    )
    config_path = tmp_path / "config.toml"
    config = load_config(config_path)
    assert config.video_generation.providers.tokenrhythm.api_key == "synthetic-env-only-key"
    assert (
        "video_generation.providers.tokenrhythm.api_key" in config._runtime_secret_paths
    )
    from opensquilla.tools.builtin import media

    status = media.video_generation_credential_status(config, provider_id="tokenrhythm")
    assert status["source"] == "video_env_injected_direct"
    assert status["envKey"] == "OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__TOKENRHYTHM__API_KEY"
    assert status["clearable"] is False
    config.video_generation.provider = "tokenrhythm"
    config.video_generation.primary = "wan3.0-video"
    persist_config(config, path=config_path, backup=False)
    contents = config_path.read_text()
    assert "synthetic-env-only-key" not in contents
    stored = tomllib.loads(contents)["video_generation"]["providers"]["tokenrhythm"]
    assert "api_key" not in stored
    assert stored["api_key_base_url"] == VIDEO_GENERATION_OFFICIAL_BASE_URLS["tokenrhythm"]


async def test_environment_video_key_keeps_original_origin_after_url_patch_and_restart(
    tmp_path, monkeypatch
) -> None:
    from opensquilla.onboarding.config_store import load_config, persist_config
    from opensquilla.tools.builtin import media

    key_env = "OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__TOKENRHYTHM__API_KEY"
    monkeypatch.setenv(key_env, "synthetic-env-video-key")
    config_path = tmp_path / "config.toml"
    config = load_config(config_path)
    monkeypatch.setattr(media, "configure_video_generation", lambda *_args, **_kwargs: None)

    await _handle_config_patch(
        {
            "patches": {
                "video_generation.providers.tokenrhythm.base_url": (
                    "https://new-video-origin.example/v1"
                )
            }
        },
        SimpleNamespace(config=config),
    )
    stored = tomllib.loads(config_path.read_text())["video_generation"]["providers"][
        "tokenrhythm"
    ]
    assert "api_key" not in stored
    assert stored["api_key_base_url"] == VIDEO_GENERATION_OFFICIAL_BASE_URLS["tokenrhythm"]
    assert media.video_generation_credential_status(
        config, provider_id="tokenrhythm"
    )["available"] is False

    reloaded = load_config(config_path)
    assert reloaded.video_generation.providers.tokenrhythm.api_key == "synthetic-env-video-key"
    assert media.video_generation_credential_status(
        reloaded, provider_id="tokenrhythm"
    )["available"] is False

    # The binding must also survive a save while the environment key is absent.
    monkeypatch.delenv(key_env)
    missing_key = load_config(config_path)
    persist_config(missing_key, path=config_path, backup=False)
    monkeypatch.setenv(key_env, "synthetic-env-video-key")
    restored_key = load_config(config_path)
    assert media.video_generation_credential_status(
        restored_key, provider_id="tokenrhythm"
    )["available"] is False


async def test_env_injected_video_key_stays_bound_when_switching_url_and_env_reference(
    tmp_path, monkeypatch
) -> None:
    from opensquilla.onboarding.config_store import load_config
    from opensquilla.tools.builtin import media

    key_env = "OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__TOKENRHYTHM__API_KEY"
    monkeypatch.setenv(key_env, "synthetic-old-origin-key")
    monkeypatch.setenv("VIDEO_PROXY_KEY", "synthetic-new-origin-key")
    config_path = tmp_path / "config.toml"
    config = load_config(config_path)
    monkeypatch.setattr(media, "configure_video_generation", lambda *_args, **_kwargs: None)

    await _handle_config_patch(
        {
            "patches": {
                "video_generation.providers.tokenrhythm.base_url": (
                    "https://new-video-origin.example/v1"
                ),
                "video_generation.providers.tokenrhythm.api_key_env": "VIDEO_PROXY_KEY",
            }
        },
        SimpleNamespace(config=config),
    )
    provider = config.video_generation.providers.tokenrhythm
    assert provider.api_key == "synthetic-old-origin-key"
    assert provider.api_key_base_url == VIDEO_GENERATION_OFFICIAL_BASE_URLS["tokenrhythm"]
    current_status = media.video_generation_credential_status(config, provider_id="tokenrhythm")
    assert current_status["source"] == "video_env"
    assert current_status["envKey"] == "VIDEO_PROXY_KEY"
    stored = tomllib.loads(config_path.read_text())["video_generation"]["providers"][
        "tokenrhythm"
    ]
    assert "api_key" not in stored
    assert stored["api_key_base_url"] == VIDEO_GENERATION_OFFICIAL_BASE_URLS["tokenrhythm"]

    reloaded = load_config(config_path)
    assert reloaded.video_generation.providers.tokenrhythm.api_key == "synthetic-old-origin-key"
    assert media.video_generation_credential_status(
        reloaded, provider_id="tokenrhythm"
    ) == current_status


async def test_env_injected_video_key_wins_same_origin_env_patch_and_cannot_be_cleared(
    tmp_path, monkeypatch
) -> None:
    from opensquilla.onboarding.config_store import load_config
    from opensquilla.tools.builtin import media

    monkeypatch.setenv(
        "OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__TOKENRHYTHM__API_KEY",
        "synthetic-injected-direct-key",
    )
    monkeypatch.setenv("VIDEO_PROXY_KEY", "synthetic-other-env-key")
    config_path = tmp_path / "config.toml"
    config = load_config(config_path)
    monkeypatch.setattr(media, "configure_video_generation", lambda *_args, **_kwargs: None)
    context = SimpleNamespace(config=config)

    await _handle_config_patch(
        {"patches": {"video_generation.providers.tokenrhythm.api_key_env": "VIDEO_PROXY_KEY"}},
        context,
    )
    status = media.video_generation_credential_status(config, provider_id="tokenrhythm")
    assert status["source"] == "video_env_injected_direct"
    assert status["clearable"] is False
    await _handle_config_patch(
        {"patches": {"video_generation.providers.tokenrhythm.api_key": ""}},
        context,
    )
    assert media.video_generation_credential_status(config, provider_id="tokenrhythm") == status
    reloaded = load_config(config_path)
    assert media.video_generation_credential_status(reloaded, provider_id="tokenrhythm") == status


async def test_masked_nested_env_key_does_not_follow_cleared_file_key_to_new_origin(
    tmp_path, monkeypatch
) -> None:
    from opensquilla.onboarding.config_store import load_config
    from opensquilla.tools.builtin import media

    monkeypatch.setenv(
        "OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__TOKENRHYTHM__API_KEY",
        "synthetic-masked-env-key",
    )
    monkeypatch.setenv("VIDEO_PROXY_KEY", "synthetic-new-origin-key")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[video_generation.providers.tokenrhythm]\napi_key = "synthetic-file-key"\n'
    )
    config = load_config(config_path)
    monkeypatch.setattr(media, "configure_video_generation", lambda *_args, **_kwargs: None)
    assert config.video_generation.providers.tokenrhythm.api_key == "synthetic-file-key"

    await _handle_config_patch(
        {
            "patches": {
                "video_generation.providers.tokenrhythm.base_url": (
                    "https://new-video-origin.example/v1"
                ),
                "video_generation.providers.tokenrhythm.api_key_env": "VIDEO_PROXY_KEY",
            }
        },
        SimpleNamespace(config=config),
    )
    provider = config.video_generation.providers.tokenrhythm
    assert provider.api_key == "synthetic-masked-env-key"
    assert provider.api_key_base_url == VIDEO_GENERATION_OFFICIAL_BASE_URLS["tokenrhythm"]
    current_status = media.video_generation_credential_status(config, provider_id="tokenrhythm")
    assert current_status["source"] == "video_env"
    assert current_status["envKey"] == "VIDEO_PROXY_KEY"
    stored = tomllib.loads(config_path.read_text())["video_generation"]["providers"][
        "tokenrhythm"
    ]
    assert "api_key" not in stored
    assert stored["api_key_base_url"] == VIDEO_GENERATION_OFFICIAL_BASE_URLS["tokenrhythm"]
    reloaded = load_config(config_path)
    assert media.video_generation_credential_status(reloaded, provider_id="tokenrhythm") == (
        current_status
    )


async def test_manual_video_key_without_binding_keeps_original_origin_after_url_patch(
    tmp_path, monkeypatch
) -> None:
    from opensquilla.onboarding.config_store import load_config
    from opensquilla.tools.builtin import media

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[video_generation.providers.tokenrhythm]\napi_key = "synthetic-manual-key"\n'
    )
    config = load_config(config_path)
    assert (
        config.video_generation.providers.tokenrhythm.api_key_base_url
        == VIDEO_GENERATION_OFFICIAL_BASE_URLS["tokenrhythm"]
    )
    monkeypatch.setattr(media, "configure_video_generation", lambda *_args, **_kwargs: None)

    await _handle_config_patch(
        {
            "patches": {
                "video_generation.providers.tokenrhythm.base_url": (
                    "https://new-video-origin.example/v1"
                )
            }
        },
        SimpleNamespace(config=config),
    )
    reloaded = load_config(config_path)
    assert reloaded.video_generation.providers.tokenrhythm.api_key == "synthetic-manual-key"
    assert media.video_generation_credential_status(
        reloaded, provider_id="tokenrhythm"
    )["available"] is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_duration_seconds": 3}, "max_duration_seconds must be at least 4"),
        ({"duration_seconds": 5}, "duration_seconds must be 4, 6, or 8"),
        ({"resolution": "1080p", "max_duration_seconds": 6}, "1080p requires max_duration_seconds"),
        ({"resolution": "1080p", "duration_seconds": 6}, "1080p requires duration_seconds"),
    ],
)
def test_gemini_video_generation_rejects_unsupported_duration_settings(
    overrides: dict, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        VideoGenerationConfig(
            enabled=True,
            provider="gemini",
            primary="veo-3.1-generate-preview",
            **overrides,
        )


@pytest.mark.parametrize(
    ("provider", "model", "minimum", "maximum"),
    [
        ("xai", "grok-imagine-video-1.5", 1, 15),
        ("qwen", "wan2.7-t2v", 2, 15),
        ("qwen_token_plan", "wan2.7-t2v", 2, 15),
        ("tokenrhythm", "wan3.0-video", 2, 30),
    ],
)
def test_video_generation_duration_matches_provider_adapter_limits(
    provider: str, model: str, minimum: int, maximum: int
) -> None:
    with pytest.raises(ValidationError):
        VideoGenerationConfig(
            enabled=True, provider=provider, primary=model, max_duration_seconds=minimum - 1
        )
    if minimum > 1:
        with pytest.raises(ValidationError, match=f"duration_seconds must be at least {minimum}"):
            VideoGenerationConfig(
                enabled=True, provider=provider, primary=model, duration_seconds=minimum - 1
            )
    with pytest.raises(ValidationError, match=f"duration_seconds must be at most {maximum}"):
        VideoGenerationConfig(
            enabled=True,
            provider=provider,
            primary=model,
            max_duration_seconds=60,
            duration_seconds=maximum + 1,
        )
    accepted = VideoGenerationConfig(
        enabled=True,
        provider=provider,
        primary=model,
        max_duration_seconds=maximum,
        duration_seconds=maximum,
    )
    assert accepted.duration_seconds == maximum
    compatible = VideoGenerationConfig(
        enabled=True, provider=provider, primary=model, max_duration_seconds=60
    )
    assert compatible.max_duration_seconds == 60


def test_gemini_video_generation_accepts_legacy_large_limit_without_explicit_duration() -> None:
    config = VideoGenerationConfig(
        enabled=True,
        provider="gemini",
        primary="veo-3.1-generate-preview",
        max_duration_seconds=60,
    )
    assert config.max_duration_seconds == 60


def test_xai_original_grok_model_rejects_1080p_default() -> None:
    with pytest.raises(ValidationError, match="grok-imagine-video supports at most 720p"):
        VideoGenerationConfig(
            enabled=True, provider="xai", primary="grok-imagine-video", resolution="1080p"
        )
    allowed = VideoGenerationConfig(
        enabled=True, provider="xai", primary="grok-imagine-video-1.5", resolution="1080p"
    )
    assert allowed.resolution == "1080p"


@pytest.mark.parametrize(
    "draft",
    [
        {"provider": "tokenrhythm", "primary": "wan3.0-video", "max_duration_seconds": 1},
        {
            "provider": "qwen_token_plan",
            "primary": "happyhorse-1.1-t2v",
            "max_duration_seconds": 2,
        },
        {
            "provider": "xai",
            "primary": "grok-imagine-video",
            "resolution": "1080p",
        },
        {
            "provider": "qwen",
            "primary": "wan2.7-t2v",
            "max_duration_seconds": 60,
            "duration_seconds": 16,
        },
    ],
)
def test_disabled_legacy_video_drafts_remain_loadable_until_enabled(draft: dict) -> None:
    gateway = GatewayConfig.model_validate({"video_generation": draft})
    assert gateway.video_generation.enabled is False
    with pytest.raises(ValidationError):
        VideoGenerationConfig.model_validate({**draft, "enabled": True})


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_duration_seconds": 4, "duration_seconds": 4},
        {"max_duration_seconds": 6, "duration_seconds": 6},
        {"resolution": "1080p", "max_duration_seconds": 8, "duration_seconds": 8},
        {"resolution": "1080p", "max_duration_seconds": 8, "duration_seconds": None},
    ],
)
def test_gemini_video_generation_accepts_supported_duration_settings(overrides: dict) -> None:
    config = VideoGenerationConfig(
        enabled=True,
        provider="gemini",
        primary="veo-3.1-generate-preview",
        **overrides,
    )

    assert config.effective_provider == "gemini"


def test_gemini_video_duration_limits_apply_before_enabling() -> None:
    with pytest.raises(ValidationError, match="max_duration_seconds must be at least 4"):
        VideoGenerationConfig(provider="gemini", max_duration_seconds=3)


def test_legacy_openrouter_video_route_keeps_its_duration_rules() -> None:
    config = VideoGenerationConfig(
        enabled=True,
        primary="google/veo-3.1-fast",
        max_duration_seconds=3,
        duration_seconds=2,
        resolution="1080p",
    )

    assert config.effective_provider == "openrouter"
    assert config.duration_seconds == 2


@pytest.mark.parametrize("provider", ["qwen", "qwen_token_plan"])
def test_happyhorse_video_requires_at_least_three_seconds(provider: str) -> None:
    with pytest.raises(ValidationError, match="at least 3"):
        VideoGenerationConfig(
            enabled=True,
            provider=provider,
            primary="happyhorse-1.1-t2v",
            max_duration_seconds=2,
        )
    with pytest.raises(ValidationError, match="at least 3"):
        VideoGenerationConfig(
            enabled=True,
            provider=provider,
            primary="happyhorse-1.1-t2v",
            duration_seconds=2,
        )


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"duration_seconds": 0}, "duration_seconds"),
        ({"duration_seconds": 61}, "duration_seconds"),
        ({"max_duration_seconds": 0}, "max_duration_seconds"),
        ({"duration_seconds": 9}, "max_duration_seconds"),
        ({"aspect_ratio": "1:1"}, "aspect_ratio"),
        ({"allowed_aspect_ratios": []}, "allowed_aspect_ratios"),
        ({"allowed_aspect_ratios": ["16:9", "16:9"]}, "allowed_aspect_ratios"),
        ({"allowed_aspect_ratios": ["9:16"]}, "allowed_aspect_ratios"),
        ({"resolution": "4k"}, "resolution"),
        ({"allowed_resolutions": []}, "allowed_resolutions"),
        ({"allowed_resolutions": ["720p", "720p"]}, "allowed_resolutions"),
        ({"allowed_resolutions": ["1080p"]}, "allowed_resolutions"),
        ({"timeout_seconds": 29}, "timeout_seconds"),
        ({"timeout_seconds": 1801}, "timeout_seconds"),
        ({"max_output_bytes": 0}, "max_output_bytes"),
        ({"max_output_bytes": 500 * 1024 * 1024 + 1}, "max_output_bytes"),
    ],
)
def test_video_generation_rejects_out_of_bounds_parameters(overrides: dict, field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        VideoGenerationConfig(**overrides)


def test_video_generation_accepts_nested_settings_and_round_trips() -> None:
    config = GatewayConfig.model_validate(
        {
            "video_generation": {
                "enabled": True,
                "primary": "google/veo-3.1-fast",
                "duration_seconds": 8,
                "max_duration_seconds": 8,
                "aspect_ratio": "9:16",
                "allowed_aspect_ratios": ["9:16"],
                "resolution": "1080p",
                "allowed_resolutions": ["1080p"],
                "timeout_seconds": 900,
                "max_output_bytes": 250 * 1024 * 1024,
            }
        }
    )

    stored = config.to_toml_dict()["video_generation"]
    assert stored["enabled"] is True
    assert config.video_generation.effective_provider == "openrouter"
    assert stored["primary"] == "google/veo-3.1-fast"
    assert stored["provider"] == ""
    assert stored["duration_seconds"] == 8
    assert stored["max_duration_seconds"] == 8
    assert stored["aspect_ratio"] == "9:16"
    assert stored["allowed_aspect_ratios"] == ["9:16"]
    assert stored["resolution"] == "1080p"
    assert stored["allowed_resolutions"] == ["1080p"]
    assert stored["timeout_seconds"] == 900
    assert stored["max_output_bytes"] == 250 * 1024 * 1024


def test_video_generation_explicit_gemini_provider_round_trips() -> None:
    config = GatewayConfig.model_validate(
        {
            "video_generation": {
                "enabled": True,
                "provider": "gemini",
                "primary": "veo-3.1-generate-preview",
            }
        }
    )

    assert config.video_generation.effective_provider == "gemini"
    stored = config.to_toml_dict()["video_generation"]
    assert stored["provider"] == "gemini"
    assert stored["primary"] == "veo-3.1-generate-preview"


def test_video_generation_custom_endpoint_and_env_reference_round_trip() -> None:
    config = GatewayConfig.model_validate(
        {
            "video_generation": {
                "enabled": True,
                "provider": "qwen",
                "primary": "wan2.6-t2v",
                "providers": {
                    "qwen": {
                        "base_url": "https://workspace.example/api/v1",
                        "api_key_env": "VIDEO_QWEN_API_KEY",
                    }
                },
            }
        }
    )

    stored = config.to_toml_dict()["video_generation"]
    assert stored["provider"] == "qwen"
    assert stored["providers"]["qwen"] == {
        "base_url": "https://workspace.example/api/v1",
        "api_key_env": "VIDEO_QWEN_API_KEY",
    }
    assert (
        config.to_public_dict()["video_generation"]["providers"]["qwen"]
        == (stored["providers"]["qwen"])
    )


def test_video_generation_accepts_environment_configuration(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_VIDEO_GENERATION_ENABLED", "true")
    monkeypatch.setenv("OPENSQUILLA_VIDEO_GENERATION_PRIMARY", "google/veo-3.1-fast")
    monkeypatch.setenv("OPENSQUILLA_VIDEO_GENERATION_MAX_DURATION_SECONDS", "6")

    config = GatewayConfig()

    assert config.video_generation.enabled is True
    assert config.video_generation.effective_provider == "openrouter"
    assert config.video_generation.primary == "google/veo-3.1-fast"
    assert config.video_generation.max_duration_seconds == 6


def test_video_generation_accepts_explicit_gemini_environment_configuration(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_VIDEO_GENERATION_ENABLED", "true")
    monkeypatch.setenv("OPENSQUILLA_VIDEO_GENERATION_PROVIDER", "gemini")
    monkeypatch.setenv("OPENSQUILLA_VIDEO_GENERATION_PRIMARY", "veo-3.1-generate-preview")

    config = GatewayConfig()

    assert config.video_generation.provider == "gemini"
    assert config.video_generation.effective_provider == "gemini"
    assert config.video_generation.primary == "veo-3.1-generate-preview"


def test_video_generation_accepts_nested_provider_environment_configuration(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_VIDEO_GENERATION_PROVIDER", "xai")
    monkeypatch.setenv("OPENSQUILLA_VIDEO_GENERATION_PRIMARY", "grok-imagine-video")
    monkeypatch.setenv(
        "OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__XAI__BASE_URL", "https://xai-proxy.example/v1"
    )
    monkeypatch.setenv(
        "OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__XAI__API_KEY_ENV", "VIDEO_XAI_API_KEY"
    )

    config = GatewayConfig()

    assert config.video_generation.provider == "xai"
    assert config.video_generation.providers.xai.base_url == "https://xai-proxy.example/v1"
    assert config.video_generation.providers.xai.api_key_env == "VIDEO_XAI_API_KEY"


def test_video_generation_is_synced_with_runtime_media_config(monkeypatch) -> None:
    from opensquilla.tools.builtin import media

    config = GatewayConfig()
    observed: list[tuple[object, object]] = []
    monkeypatch.setattr(media, "configure_image_generation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(media, "configure_audio", lambda *_args: None)
    monkeypatch.setattr(
        media,
        "configure_video_generation",
        lambda video, *, gateway_config: observed.append((video, gateway_config)),
        raising=False,
    )

    sync_media_runtime(config)

    assert observed == [(config.video_generation, config)]


async def test_video_generation_config_patch_is_live_and_persisted(tmp_path, monkeypatch) -> None:
    from opensquilla.tools.builtin import media

    config_path = tmp_path / "config.toml"
    config = GatewayConfig(config_path=str(config_path))
    observed: list[object] = []
    monkeypatch.setattr(
        media,
        "configure_video_generation",
        lambda video, *, gateway_config: observed.append(video),
    )

    result = await _handle_config_patch(
        {
            "patch": {
                "video_generation": {
                    "enabled": True,
                    "primary": "google/veo-3.1-fast",
                    "duration_seconds": 8,
                }
            }
        },
        SimpleNamespace(config=config),
    )

    assert result["restartRequired"] is False
    assert "video_generation" in result["liveApplied"]
    assert observed[-1] is not None
    assert observed[-1].effective_provider == "openrouter"
    assert observed[-1].primary == "google/veo-3.1-fast"
    assert config.video_generation.primary == "google/veo-3.1-fast"
    stored = tomllib.loads(config_path.read_text())["video_generation"]
    assert stored["enabled"] is True
    assert stored["duration_seconds"] == 8

    await _handle_config_patch(
        {"patch": {"video_generation": {"duration_seconds": None}}},
        SimpleNamespace(config=config),
    )
    assert config.video_generation.duration_seconds is None
    stored = tomllib.loads(config_path.read_text())["video_generation"]
    assert "duration_seconds" not in stored


async def test_video_generation_patch_can_switch_provider_and_model_together(
    tmp_path, monkeypatch
) -> None:
    from opensquilla.tools.builtin import media

    config_path = tmp_path / "config.toml"
    config = GatewayConfig(config_path=str(config_path))
    monkeypatch.setattr(media, "configure_video_generation", lambda *_args, **_kwargs: None)

    result = await _handle_config_patch(
        {
            "patch": {
                "video_generation": {
                    "enabled": True,
                    "provider": "gemini",
                    "primary": "veo-3.1-generate-preview",
                }
            }
        },
        SimpleNamespace(config=config),
    )

    assert result["restartRequired"] is False
    assert config.video_generation.effective_provider == "gemini"
    assert config.video_generation.primary == "veo-3.1-generate-preview"
    stored = tomllib.loads(config_path.read_text())["video_generation"]
    assert stored["provider"] == "gemini"
    assert stored["primary"] == "veo-3.1-generate-preview"


async def test_video_dot_patch_uses_existing_tokenrhythm_image_key(
    tmp_path, monkeypatch
) -> None:
    from opensquilla.tools.builtin import media

    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    config = GatewayConfig.model_validate(
        {
            "config_path": str(tmp_path / "config.toml"),
            "image_generation": {
                "providers": {"tokenrhythm": {"api_key": "synthetic-image-key"}}
            },
        }
    )
    try:
        await _handle_config_patch(
            {
                "patches": {
                    "video_generation.enabled": True,
                    "video_generation.provider": "tokenrhythm",
                    "video_generation.primary": "wan3.0-video",
                }
            },
            SimpleNamespace(config=config),
        )
        assert config.video_generation.enabled is True
        assert media.video_generation_available() is True
        status = media.video_generation_credential_status(
            config, provider_id="tokenrhythm"
        )
        assert status["source"] == "image_direct"
        assert status["owner"] == "image"
    finally:
        media.configure_video_generation(None)


async def test_video_generation_patch_can_set_selected_provider_endpoint(
    tmp_path, monkeypatch
) -> None:
    from opensquilla.tools.builtin import media

    config_path = tmp_path / "config.toml"
    config = GatewayConfig(config_path=str(config_path))
    monkeypatch.setattr(media, "configure_video_generation", lambda *_args, **_kwargs: None)

    result = await _handle_config_patch(
        {
            "patch": {
                "video_generation": {
                    "provider": "xai",
                    "primary": "grok-imagine-video",
                    "providers": {
                        "xai": {
                            "base_url": "https://video-proxy.example/v1",
                            "api_key_env": "VIDEO_XAI_API_KEY",
                        }
                    },
                }
            }
        },
        SimpleNamespace(config=config),
    )

    assert result["restartRequired"] is False
    assert config.video_generation.providers.xai.base_url == "https://video-proxy.example/v1"
    stored = tomllib.loads(config_path.read_text())["video_generation"]
    assert stored["providers"]["xai"]["base_url"] == "https://video-proxy.example/v1"
    assert stored["providers"]["xai"]["api_key_env"] == "VIDEO_XAI_API_KEY"


async def test_video_direct_key_remains_bound_across_url_and_credential_patches(
    tmp_path, monkeypatch
) -> None:
    from opensquilla.tools.builtin import media

    config_path = tmp_path / "config.toml"
    config = GatewayConfig(config_path=str(config_path))
    monkeypatch.setattr(media, "configure_video_generation", lambda *_args, **_kwargs: None)
    context = SimpleNamespace(config=config)

    await _handle_config_patch(
        {
            "patch": {
                "video_generation": {
                    "provider": "tokenrhythm",
                    "primary": "wan3.0-video",
                    "providers": {"tokenrhythm": {"api_key": "synthetic-old-key"}},
                }
            }
        },
        context,
    )
    provider = config.video_generation.providers.tokenrhythm
    assert provider.api_key_base_url == VIDEO_GENERATION_OFFICIAL_BASE_URLS["tokenrhythm"]
    assert media.video_generation_credential_status(
        config, provider_id="tokenrhythm"
    )["available"] is True

    await _handle_config_patch(
        {
            "patches": {
                "video_generation.providers.tokenrhythm.base_url": "https://video-proxy.example/v1"
            }
        },
        context,
    )
    provider = config.video_generation.providers.tokenrhythm
    assert provider.api_key == "synthetic-old-key"
    assert provider.api_key_base_url == VIDEO_GENERATION_OFFICIAL_BASE_URLS["tokenrhythm"]
    assert media.video_generation_credential_status(
        config, provider_id="tokenrhythm"
    )["available"] is False

    await _handle_config_patch(
        {
            "patch": {
                "video_generation": {
                    "providers": {
                        "tokenrhythm": {
                            "base_url": "https://second-proxy.example/v1",
                            "api_key": "synthetic-new-key",
                        }
                    }
                }
            }
        },
        context,
    )
    provider = config.video_generation.providers.tokenrhythm
    assert provider.api_key == "synthetic-new-key"
    assert provider.api_key_base_url == "https://second-proxy.example/v1"
    assert media.video_generation_credential_status(
        config, provider_id="tokenrhythm"
    )["available"] is True
    stored = tomllib.loads(config_path.read_text())["video_generation"]["providers"][
        "tokenrhythm"
    ]
    assert stored["api_key_base_url"] == "https://second-proxy.example/v1"

    monkeypatch.setenv("VIDEO_TOKENRHYTHM_API_KEY", "synthetic-env-key")
    await _handle_config_patch(
        {
            "patches": {
                "video_generation.providers.tokenrhythm.api_key_env": "VIDEO_TOKENRHYTHM_API_KEY"
            }
        },
        context,
    )
    provider = config.video_generation.providers.tokenrhythm
    assert provider.api_key == ""
    assert provider.api_key_base_url == ""
    status = media.video_generation_credential_status(config, provider_id="tokenrhythm")
    assert status["source"] == "video_env"
    assert status["owner"] == "video"
    stored = tomllib.loads(config_path.read_text())["video_generation"]["providers"][
        "tokenrhythm"
    ]
    assert "api_key" not in stored
    assert "api_key_base_url" not in stored

    await _handle_config_patch(
        {
            "patches": {
                "video_generation.providers.tokenrhythm.api_key_base_url": (
                    "https://second-proxy.example/v1"
                )
            }
        },
        context,
    )
    assert config.video_generation.providers.tokenrhythm.api_key_base_url == ""


async def test_video_url_and_explicit_env_patch_replaces_old_direct_key(
    tmp_path, monkeypatch
) -> None:
    from opensquilla.tools.builtin import media

    config = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    monkeypatch.setattr(media, "configure_video_generation", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("VIDEO_PROXY_KEY", "synthetic-proxy-key")
    context = SimpleNamespace(config=config)
    await _handle_config_patch(
        {"patches": {"video_generation.providers.tokenrhythm.api_key": "synthetic-old-key"}},
        context,
    )

    await _handle_config_patch(
        {
            "patches": {
                "video_generation.providers.tokenrhythm.base_url": (
                    "https://video-proxy.example/v1"
                ),
                "video_generation.providers.tokenrhythm.api_key_env": "VIDEO_PROXY_KEY",
            }
        },
        context,
    )
    provider = config.video_generation.providers.tokenrhythm
    assert provider.api_key == ""
    assert provider.api_key_base_url == ""
    assert media.video_generation_credential_status(config, provider_id="tokenrhythm") == {
        "providerId": "tokenrhythm",
        "available": True,
        "source": "video_env",
        "owner": "video",
        "envKey": "VIDEO_PROXY_KEY",
        "clearable": False,
    }


async def test_clearing_video_direct_key_reuses_matching_image_key(
    tmp_path, monkeypatch
) -> None:
    from opensquilla.tools.builtin import media

    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    config = GatewayConfig.model_validate(
        {
            "config_path": str(tmp_path / "config.toml"),
            "image_generation": {
                "providers": {"tokenrhythm": {"api_key": "synthetic-image-key"}}
            },
        }
    )
    monkeypatch.setattr(media, "configure_video_generation", lambda *_args, **_kwargs: None)
    context = SimpleNamespace(config=config)
    await _handle_config_patch(
        {"patches": {"video_generation.providers.tokenrhythm.api_key": "synthetic-video-key"}},
        context,
    )
    assert media.video_generation_credential_status(config, provider_id="tokenrhythm")[
        "source"
    ] == "video_direct"

    await _handle_config_patch(
        {"patches": {"video_generation.providers.tokenrhythm.api_key": ""}},
        context,
    )
    provider = config.video_generation.providers.tokenrhythm
    assert provider.api_key == ""
    assert provider.api_key_base_url == ""
    assert media.video_generation_credential_status(config, provider_id="tokenrhythm") == {
        "providerId": "tokenrhythm",
        "available": True,
        "source": "image_direct",
        "owner": "image",
        "envKey": "",
        "clearable": False,
    }


async def test_video_generation_custom_endpoint_does_not_author_default_env(
    tmp_path, monkeypatch
) -> None:
    from opensquilla.tools.builtin import media

    config_path = tmp_path / "config.toml"
    config = GatewayConfig(config_path=str(config_path))
    monkeypatch.setattr(media, "configure_video_generation", lambda *_args, **_kwargs: None)

    await _handle_config_patch(
        {
            "patch": {
                "video_generation": {
                    "provider": "xai",
                    "primary": "grok-imagine-video",
                    "providers": {"xai": {"base_url": "https://video-proxy.example/v1"}},
                }
            }
        },
        SimpleNamespace(config=config),
    )

    stored = tomllib.loads(config_path.read_text())["video_generation"]["providers"]["xai"]
    assert stored == {"base_url": "https://video-proxy.example/v1"}
    assert "api_key_env" in config.video_generation.providers.xai.model_fields_set
    assert "api_key_env" not in config._persist_raw_base["video_generation"]["providers"]["xai"]
