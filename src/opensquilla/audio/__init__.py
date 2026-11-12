"""Speech capability modules shared by the gateway, tools, and CLI.

``audio.stt_types`` defines the provider-neutral transcription contract;
``audio.stt_registry`` builds an ordered provider chain from gateway config.
"""

from opensquilla.audio.stt_registry import (
    SttConfigError,
    build_stt_chain,
    resolve_stt_api_key_env,
)
from opensquilla.audio.stt_types import SttAudio, SttProvider, SttResult

__all__ = [
    "SttAudio",
    "SttConfigError",
    "SttProvider",
    "SttResult",
    "build_stt_chain",
    "resolve_stt_api_key_env",
]
