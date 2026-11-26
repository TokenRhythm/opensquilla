"""Speech capability modules shared by the gateway, tools, and CLI.

``audio.stt_types`` defines the provider-neutral transcription contract;
``audio.stt_registry`` builds an ordered provider chain from gateway config;
``audio.voice_presets`` resolves named TTS parameter packs (PRD F3.6).
"""

from opensquilla.audio.stt_registry import (
    SttConfigError,
    build_stt_chain,
    resolve_stt_api_key_env,
)
from opensquilla.audio.stt_types import SttAudio, SttProvider, SttResult
from opensquilla.audio.voice_presets import (
    ResolvedVoice,
    VoicePresetError,
    list_presets,
    resolve_voice,
)

__all__ = [
    "ResolvedVoice",
    "SttAudio",
    "SttConfigError",
    "SttProvider",
    "SttResult",
    "VoicePresetError",
    "build_stt_chain",
    "list_presets",
    "resolve_stt_api_key_env",
    "resolve_voice",
]
