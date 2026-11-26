"""ElevenLabs Scribe adapter for the provider-neutral STT contract.

Delegates to the existing :class:`~opensquilla.provider.audio.
ElevenLabsAudioProductionProvider` client so the WebUI microphone path keeps
its exact metering and response semantics.
"""

from __future__ import annotations

import httpx

from opensquilla.audio.stt_types import SttAudio, SttResult
from opensquilla.provider.audio import (
    ElevenLabsAudioProductionProvider,
    ElevenLabsSpeechToTextRequest,
)


class ElevenLabsSttProvider:
    """Transcription through ElevenLabs ``/v1/speech-to-text``."""

    provider_id = "elevenlabs"
    accepts_request_model = True

    def __init__(
        self,
        *,
        api_key: str = "",
        api_key_env: str = "ELEVENLABS_API_KEY",
        base_url: str = "https://api.elevenlabs.io",
        default_model: str = "scribe_v2",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = ElevenLabsAudioProductionProvider(
            api_key=api_key or None,
            api_key_env=api_key_env,
            base_url=base_url,
            transport=transport,
        )
        self._default_model = default_model or "scribe_v2"

    async def transcribe(
        self,
        audio: SttAudio,
        *,
        model_id: str | None = None,
        language: str | None = None,
    ) -> SttResult:
        request = ElevenLabsSpeechToTextRequest(
            audio_bytes=audio.data,
            filename=audio.filename,
            mime_type=audio.mime_type,
            model_id=str(model_id or self._default_model),
            language_code=language or None,
        )
        result = await self._client.transcribe_audio(request)
        return SttResult(
            text=result.text,
            provider=result.provider,
            model=result.model,
            language_code=result.language_code,
            language_probability=result.language_probability,
        )
