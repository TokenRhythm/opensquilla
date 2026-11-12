"""OpenAI-compatible speech-to-text providers.

Two wire protocols are covered, mirroring what ASR endpoints actually
expose today:

* ``openai_transcriptions`` — multipart upload to ``/audio/transcriptions``
  returning ``{"text": ...}`` (OpenAI Whisper, SiliconFlow, Groq, ...).
* ``openai_chat`` — audio inlined as an ``input_audio`` content part of a
  chat completion (MiMo ``mimo-v2.5-asr`` and similar, which share one
  platform key with chat/TTS).

Both are metered through the shared accounting boundary like ElevenLabs.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os

import httpx

from opensquilla.audio.stt_types import SttAudio, SttProviderError, SttResult
from opensquilla.env import trust_env as _trust_env
from opensquilla.provider.metered_http import accounted_request, audio_duration_seconds
from opensquilla.secrets import clean_header_secret
from opensquilla.usage_metering import UsageMeter

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_TIMEOUT_SECONDS = 120.0
_MIME_FORMATS = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/flac": "flac",
    "audio/webm": "webm",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/amr": "amr",
    "audio/3gpp": "3gp",
}
# ISO-639-1 codes every /audio/transcriptions endpoint understands; other
# hints (auto, cantonese, ...) are left out so the endpoint guesses instead
# of rejecting the field.
_SAFE_LANGUAGE_CODES = frozenset(
    {
        "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "br", "bs", "ca",
        "cs", "cy", "da", "de", "el", "en", "es", "et", "eu", "fa", "fi", "fo",
        "fr", "gl", "gu", "ha", "haw", "he", "hi", "hr", "ht", "hu", "hy", "id",
        "is", "it", "ja", "jw", "ka", "kk", "km", "kn", "ko", "la", "lb", "ln",
        "lo", "lt", "lv", "mg", "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my",
        "ne", "nl", "nn", "no", "oc", "pa", "pl", "ps", "pt", "ro", "ru", "sa",
        "sd", "si", "sk", "sl", "sn", "so", "sq", "sr", "su", "sv", "sw", "ta",
        "te", "tg", "th", "tk", "tl", "tr", "tt", "uk", "ur", "uz", "vi", "yi",
        "yo", "zh",
    }
)


def _normalize_base_url(base_url: str) -> str:
    return (base_url or _DEFAULT_BASE_URL).rstrip("/")


def _audio_format(audio: SttAudio) -> str:
    mime = (audio.mime_type or "").split(";", 1)[0].strip().lower()
    if mime in _MIME_FORMATS:
        return _MIME_FORMATS[mime]
    suffix = (audio.filename.rsplit(".", 1)[-1] if "." in audio.filename else "").lower()
    if suffix in {"m4a"}:
        return "m4a"
    return suffix or "wav"


def _error_message(raw_body: bytes, status: int, reason: str, provider_id: str) -> str:
    detail = ""
    try:
        payload = json.loads(raw_body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or "").strip()
        elif isinstance(err, str):
            detail = err.strip()
        if not detail:
            detail = str(payload.get("message") or "").strip()
    if not detail:
        text = raw_body.decode("utf-8", "replace").strip()
        detail = text[:1000] if text else "<empty body>"
    return f"{provider_id} returned HTTP {status} {reason}: {detail}"


def _stringify_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts).strip()
    return ""


class _OpenAISttBase:
    accepts_request_model = False

    def __init__(
        self,
        *,
        api_key: str,
        api_key_env: str,
        base_url: str,
        model: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = str(api_key or "")
        self._api_key_env = str(api_key_env or "")
        self._base_url = _normalize_base_url(base_url)
        self._model = str(model or "")
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport

    def _resolve_api_key(self) -> str:
        key = clean_header_secret(
            self._api_key or os.environ.get(self._api_key_env, ""),
            label="OpenAI-compatible STT API key",
        )
        if key:
            return key
        if self._api_key_env:
            raise SttProviderError(f"{self._api_key_env} is not set")
        raise SttProviderError(
            "no API key configured for "
            f"{self._base_url}; set [audio.stt.{self.provider_id}] api_key_env "
            "to the variable holding the key"
        )

    async def _request(
        self, url: str, audio: SttAudio, *, provider_kwargs: dict
    ) -> httpx.Response:
        api_key = self._resolve_api_key()
        if not self._model:
            raise SttProviderError(
                f"[audio.stt.{self.provider_id}].model is not configured"
            )
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,
            trust_env=_trust_env(),
        ) as client:
            response = await accounted_request(
                client,
                "POST",
                url,
                provider="openai",
                model=self._model,
                operation="speech_to_text",
                base_url=self._base_url,
                requested_meters=await self._requested_meters(audio),
                headers=headers,
                **provider_kwargs,
            )
        if response.status_code >= 400:
            raise SttProviderError(
                _error_message(
                    response.content,
                    response.status_code,
                    response.reason_phrase or "",
                    self.provider_id,
                )
            )
        return response

    async def _requested_meters(self, audio: SttAudio) -> tuple[UsageMeter, ...]:
        return ()


class OpenAITranscriptionsSttProvider(_OpenAISttBase):
    """multipart upload to ``/audio/transcriptions`` returning ``{"text"}``."""

    provider_id = "openai_transcriptions"

    async def transcribe(
        self,
        audio: SttAudio,
        *,
        model_id: str | None = None,
        language: str | None = None,
    ) -> SttResult:
        data: dict[str, str] = {"model": self._model}
        hint = (language or "").strip().lower()
        if hint in _SAFE_LANGUAGE_CODES:
            data["language"] = hint
        mime = audio.mime_type or "application/octet-stream"
        files = {"file": (audio.filename, audio.data, mime)}
        response = await self._request(
            f"{self._base_url}/audio/transcriptions",
            audio,
            provider_kwargs={"data": data, "files": files},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SttProviderError("speech to text provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SttProviderError("speech to text provider returned invalid payload")
        text = str(payload.get("text") or "").strip()
        if not text:
            raise SttProviderError("speech to text provider returned an empty transcription")
        language_code = str(payload.get("language") or "").strip() or None
        probability = payload.get("language_probability")
        return SttResult(
            text=text,
            provider=self.provider_id,
            model=self._model,
            language_code=language_code,
            language_probability=(
                float(probability) if isinstance(probability, (int, float)) else None
            ),
        )

    async def _requested_meters(self, audio: SttAudio) -> tuple[UsageMeter, ...]:
        duration = await asyncio.to_thread(audio_duration_seconds, audio.data)
        return (
            UsageMeter(
                "input_audio_seconds",
                "second",
                duration,
                known=duration is not None,
                source="observed",
            ),
        )


class OpenAIChatInputAudioSttProvider(_OpenAISttBase):
    """audio as an ``input_audio`` part of a chat completion request."""

    provider_id = "openai_chat"

    async def transcribe(
        self,
        audio: SttAudio,
        *,
        model_id: str | None = None,
        language: str | None = None,
    ) -> SttResult:
        content: list[dict] = []
        hint = (language or "").strip()
        if hint:
            content.append(
                {
                    "type": "text",
                    "text": f"Transcribe this recording verbatim (language hint: {hint}).",
                }
            )
        content.append(
            {
                "type": "input_audio",
                "input_audio": {
                    "data": base64.b64encode(audio.data).decode("ascii"),
                    "format": _audio_format(audio),
                },
            }
        )
        body = {
            "model": self._model,
            "messages": [{"role": "user", "content": content}],
        }
        response = await self._request(
            f"{self._base_url}/chat/completions",
            audio,
            provider_kwargs={"json": body},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SttProviderError("speech to text provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SttProviderError("speech to text provider returned invalid payload")
        choices = payload.get("choices")
        text = ""
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                text = _stringify_content(message.get("content"))
        if not text:
            raise SttProviderError("speech to text provider returned an empty transcription")
        return SttResult(text=text, provider=self.provider_id, model=self._model)

