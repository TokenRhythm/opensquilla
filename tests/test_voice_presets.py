"""Unit tests for named TTS voice presets (PRD F3.6) and tts preset resolution."""

from __future__ import annotations

from opensquilla.audio.voice_presets import (
    VoicePresetError,
    list_presets,
    resolve_voice,
)
from opensquilla.gateway.config import AudioVoicePresetConfig, GatewayConfig


def test_bare_tts_config_is_preset_free_fallback() -> None:
    cfg = GatewayConfig()
    resolved = resolve_voice(cfg)
    assert resolved.preset == ""
    assert resolved.voice_id == cfg.audio.tts.voice
    assert resolved.model_id == cfg.audio.tts.model
    assert resolved.provider == "elevenlabs"


def test_builtin_presets_list_and_resolve() -> None:
    cfg = GatewayConfig()
    names = [item["name"] for item in list_presets(cfg)]
    assert {"quiet-male", "bright-female", "pet-alert"} <= set(names)
    resolved = resolve_voice(cfg, "pet-alert")
    assert resolved.model_id == "eleven_turbo_v2_5"
    assert resolved.speed == 1.1


def test_user_preset_covering_builtin_name_merges_over_tts() -> None:
    cfg = GatewayConfig()
    cfg.audio.presets["pet-alert"] = AudioVoicePresetConfig(
        voice_id="CUSTOM", speed=0.9
    )
    resolved = resolve_voice(cfg, "pet-alert")
    assert resolved.voice_id == "CUSTOM"
    assert resolved.speed == 0.9
    # Field-by-field semantics: unset preset fields keep the [audio.tts]
    # fallback, not the covered built-in's values.
    assert resolved.model_id == cfg.audio.tts.model


def test_user_preset_merges_over_audio_tts_fallback() -> None:
    cfg = GatewayConfig()
    cfg.audio.tts.stability = 0.3
    cfg.audio.tts.speed = 1.2
    cfg.audio.presets["calm"] = AudioVoicePresetConfig(voice_id="V1")
    resolved = resolve_voice(cfg, "calm")
    # Preset voice wins; unset parameters keep the [audio.tts] fallback.
    assert resolved.voice_id == "V1"
    assert resolved.stability == 0.3
    assert resolved.speed == 1.2


def test_preset_overrides_each_tts_field() -> None:
    cfg = GatewayConfig()
    cfg.audio.tts.voice = "FALLBACK"
    cfg.audio.tts.model = "fallback-model"
    cfg.audio.tts.language_code = "en"
    cfg.audio.tts.stability = 0.9
    cfg.audio.tts.speed = 2.0
    cfg.audio.presets["zh"] = AudioVoicePresetConfig(
        voice_id="Z1",
        model_id="eleven_flash",
        language_code="zh",
        stability=0.2,
        similarity_boost=0.6,
        use_speaker_boost=False,
        speed=1.05,
    )
    resolved = resolve_voice(cfg, "zh")
    assert (resolved.voice_id, resolved.model_id) == ("Z1", "eleven_flash")
    assert resolved.language_code == "zh"
    assert resolved.stability == 0.2
    assert resolved.similarity_boost == 0.6
    assert resolved.use_speaker_boost is False
    assert resolved.speed == 1.05


def test_unknown_preset_lists_available() -> None:
    cfg = GatewayConfig()
    cfg.audio.presets["mine"] = AudioVoicePresetConfig(voice_id="M")
    try:
        resolve_voice(cfg, "missing")
    except VoicePresetError as exc:
        assert "missing" in str(exc) and "mine" in str(exc)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("expected VoicePresetError")


def test_preset_source_validation() -> None:
    cfg = GatewayConfig()
    cfg.audio.presets["x"] = AudioVoicePresetConfig(voice_id="V", source="user")
    assert cfg.audio.presets["x"].source == "user"
    try:
        AudioVoicePresetConfig(voice_id="V", source="nope")
    except Exception as exc:
        assert "source" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected validation error")


def test_preset_speed_range_matches_tool_contract() -> None:
    try:
        AudioVoicePresetConfig(voice_id="V", speed=5.0)
    except Exception:
        pass
    else:  # pragma: no cover
        raise AssertionError("speed above 4.0 must be rejected")
    AudioVoicePresetConfig(voice_id="V", speed=0.25)
    AudioVoicePresetConfig(voice_id="V", speed=4.0)


def test_list_presets_marks_sources() -> None:
    cfg = GatewayConfig()
    cfg.audio.presets["custom"] = AudioVoicePresetConfig(voice_id="C")
    items = {str(item["name"]): item for item in list_presets(cfg)}
    assert items["custom"]["source"] == "user"
    assert items["quiet-male"]["source"] == "builtin"
