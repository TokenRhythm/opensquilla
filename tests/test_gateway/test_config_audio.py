from __future__ import annotations

from opensquilla.gateway.config import GatewayConfig


def test_audio_config_defaults_are_disabled_and_elevenlabs_ready() -> None:
    cfg = GatewayConfig()

    assert cfg.audio.enabled is False
    assert cfg.audio.provider == "elevenlabs"
    assert cfg.audio.tts.model == "eleven_multilingual_v2"
    assert cfg.audio.tts.voice == "21m00Tcm4TlvDq8ikWAM"
    assert cfg.audio.tts.output_format == "mp3_44100_128"
    assert cfg.audio.providers.elevenlabs.base_url == "https://api.elevenlabs.io"
    assert cfg.audio.providers.elevenlabs.api_key_env == "ELEVENLABS_API_KEY"
    assert cfg.audio.providers.elevenlabs.voice_conversion_model == (
        "eleven_multilingual_sts_v2"
    )
    assert cfg.audio.providers.elevenlabs.music_model == "music_v1"
    assert cfg.audio.providers.elevenlabs.music_output_format == "mp3_44100_128"
    assert cfg.audio.providers.atlascloud.base_url == "https://api.atlascloud.ai"
    assert cfg.audio.providers.atlascloud.api_key_env == "ATLASCLOUD_API_KEY"
    assert cfg.audio.providers.atlascloud.tts_model == "elevenlabs/v3/text-to-speech"
    assert cfg.audio.providers.atlascloud.music_model == "minimax/music-2.6"


def test_audio_config_accepts_nested_elevenlabs_overrides() -> None:
    cfg = GatewayConfig.model_validate(
        {
            "audio": {
                "enabled": True,
                "tts": {
                    "voice": "voice_custom",
                    "model": "eleven_turbo_v2_5",
                    "output_format": "mp3_22050_32",
                },
                "providers": {
                    "elevenlabs": {
                        "api_key_env": "CUSTOM_ELEVENLABS_KEY",
                        "voice_conversion_model": "eleven_english_sts_v2",
                        "music_output_format": "mp3_22050_32",
                    },
                },
            }
        }
    )

    assert cfg.audio.enabled is True
    assert cfg.audio.tts.voice == "voice_custom"
    assert cfg.audio.tts.model == "eleven_turbo_v2_5"
    assert cfg.audio.tts.output_format == "mp3_22050_32"
    assert cfg.audio.providers.elevenlabs.api_key_env == "CUSTOM_ELEVENLABS_KEY"
    assert cfg.audio.providers.elevenlabs.voice_conversion_model == (
        "eleven_english_sts_v2"
    )
    assert cfg.audio.providers.elevenlabs.music_output_format == "mp3_22050_32"


def test_audio_config_to_toml_dict_omits_env_sourced_elevenlabs_api_key(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_AUDIO_PROVIDERS__ELEVENLABS__API_KEY", "el-env")

    cfg = GatewayConfig()
    audio = cfg.to_toml_dict()["audio"]

    assert cfg.audio.providers.elevenlabs.api_key == "el-env"
    assert "api_key" not in audio["providers"]["elevenlabs"]
    assert audio["providers"]["elevenlabs"]["api_key_env"] == "ELEVENLABS_API_KEY"


def test_audio_config_to_toml_dict_omits_env_sourced_atlascloud_api_key(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_AUDIO_PROVIDERS__ATLASCLOUD__API_KEY", "atlas-env")

    cfg = GatewayConfig()
    audio = cfg.to_toml_dict()["audio"]

    assert cfg.audio.providers.atlascloud.api_key == "atlas-env"
    assert "api_key" not in audio["providers"]["atlascloud"]
    assert audio["providers"]["atlascloud"]["api_key_env"] == "ATLASCLOUD_API_KEY"
