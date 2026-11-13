"""Named TTS voice presets: resolution semantics shared by tools and UI.

A preset (``[audio.presets.<name>]``, PRD F3.6) is a parameter pack that
overrides the raw ``[audio.tts]`` fallback field-by-field. It owns no
credentials: the provider block in ``[audio.providers.<provider>]`` still
supplies the key. Resolution merges three layers in order:

    built-in defaults  ←  [audio.tts]  ←  preset overrides

so adopting presets never changes existing behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opensquilla.gateway.config import GatewayConfig

# Conservative multilingual public figure kept as the built-in baseline so
# fresh installs speak with the same default the WebUI history used.
_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
_DEFAULT_MODEL_ID = "eleven_multilingual_v2"

# Built-in presets are read-only defaults: a user-declared ``[audio.presets]``
# entry with the same name covers them (PRD FR-3.6b). voice_id values point at
# ElevenLabs default/public sample voices; replace via a user preset of the
# same name without code changes.
_BUILTIN_PRESETS: dict[str, dict[str, object]] = {
    "quiet-male": {
        "label": "沉稳男声（内置）",
        "provider": "elevenlabs",
        "voice_id": _DEFAULT_VOICE_ID,
        "model_id": _DEFAULT_MODEL_ID,
        "stability": 0.75,
        "similarity_boost": 0.8,
        "source": "builtin",
    },
    "bright-female": {
        "label": "明亮女声（内置）",
        "provider": "elevenlabs",
        "voice_id": "EXAVITQu4vr4xnSDxMaL",
        "model_id": _DEFAULT_MODEL_ID,
        "stability": 0.5,
        "similarity_boost": 0.8,
        "source": "builtin",
    },
    "pet-alert": {
        "label": "桌宠播报（短促清晰）",
        "provider": "elevenlabs",
        "voice_id": _DEFAULT_VOICE_ID,
        "model_id": "eleven_turbo_v2_5",
        "stability": 0.4,
        "similarity_boost": 0.7,
        "speed": 1.1,
        "source": "builtin",
    },
}


class VoicePresetError(ValueError):
    """Preset is unknown, incomplete, or references an unusable provider."""


@dataclass(frozen=True)
class ResolvedVoice:
    """The effective TTS parameters after merging defaults, [audio.tts], preset."""

    preset: str
    provider: str
    voice_id: str
    model_id: str
    language_code: str
    stability: float | None
    similarity_boost: float | None
    style: float | None
    use_speaker_boost: bool | None
    speed: float


def list_presets(config: GatewayConfig) -> list[dict[str, object]]:
    """Presets in declaration order: name, label, provider, source, voice_id.

    User-declared entries win over built-ins with the same name (FR-3.6b).
    """

    items: list[dict[str, object]] = []
    for name, preset in config.audio.presets.items():
        items.append({
            "name": name,
            "label": str(getattr(preset, "label", "") or "") or name,
            "provider": str(getattr(preset, "provider", "") or "elevenlabs"),
            "voice_id": str(getattr(preset, "voice_id", "") or ""),
            "model_id": str(getattr(preset, "model_id", "") or ""),
            "source": str(getattr(preset, "source", "") or "user"),
        })
    declared = {str(item["name"]) for item in items}
    for name, values in _BUILTIN_PRESETS.items():
        if name in declared:
            continue
        items.append({"name": name, **values})
    return items


def resolve_voice(config: GatewayConfig, preset_name: str = "") -> ResolvedVoice:
    """Merge [audio.tts] with one named preset; empty name = pure fallback.

    Raises VoicePresetError when the named preset does not exist.
    """

    tts = config.audio.tts
    name = str(preset_name or "").strip()
    merged: dict[str, object] = {
        "voice_id": str(getattr(tts, "voice", "") or _DEFAULT_VOICE_ID),
        "model_id": str(getattr(tts, "model", "") or _DEFAULT_MODEL_ID),
        "language_code": str(getattr(tts, "language_code", "") or ""),
        "stability": getattr(tts, "stability", None),
        "similarity_boost": getattr(tts, "similarity_boost", None),
        "style": getattr(tts, "style", None),
        "use_speaker_boost": getattr(tts, "use_speaker_boost", None),
        "speed": float(getattr(tts, "speed", 1.0) or 1.0),
        "provider": "elevenlabs",
    }
    if name:
        preset = config.audio.presets.get(name)
        if preset is None:
            builtin = _BUILTIN_PRESETS.get(name)
            if builtin is None:
                known = ", ".join(sorted([*config.audio.presets, *_BUILTIN_PRESETS])) or "none"
                raise VoicePresetError(
                    f"unknown voice preset {name!r}; available: {known}"
                )
            merged.update(
                {
                    k: v
                    for k, v in builtin.items()
                    if k not in {"label", "source"}
                }
            )
        else:
            # A user-declared preset (including one covering a built-in name)
            # merges over [audio.tts] field-by-field: unset preset fields keep
            # the raw TTS fallback rather than the covered built-in's values.
            if getattr(preset, "voice_id", ""):
                merged["voice_id"] = str(preset.voice_id).strip()
            if getattr(preset, "model_id", ""):
                merged["model_id"] = str(preset.model_id).strip()
            if getattr(preset, "language_code", ""):
                merged["language_code"] = str(preset.language_code).strip()
            for field in ("stability", "similarity_boost", "style", "use_speaker_boost"):
                value = getattr(preset, field, None)
                if value is not None:
                    merged[field] = value
            if getattr(preset, "speed", None) is not None:
                merged["speed"] = float(preset.speed)
            merged["provider"] = str(getattr(preset, "provider", "") or "elevenlabs")
    if not str(merged.get("voice_id") or "").strip():
        raise VoicePresetError(
            f"voice preset {name or '[audio.tts]'} has no voice_id"
        )
    return ResolvedVoice(preset=name, **merged)  # type: ignore[arg-type]
