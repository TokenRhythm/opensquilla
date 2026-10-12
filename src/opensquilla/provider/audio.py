"""Provider adapters for production audio APIs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from opensquilla.endpoint_identity import credential_env_for_endpoint
from opensquilla.env import trust_env as _trust_env
from opensquilla.secrets import clean_header_secret

_ELEVENLABS_DEFAULT_API_KEY_ENV = "ELEVENLABS_API_KEY"
_ELEVENLABS_DEFAULT_BASE_URL = "https://api.elevenlabs.io"


def resolve_elevenlabs_api_key_env(provider_config: object | None) -> str:
    """Resolve the audio env source without moving its default across origins."""

    fields_set = getattr(provider_config, "model_fields_set", None)
    return credential_env_for_endpoint(
        configured_env=str(
            getattr(provider_config, "api_key_env", _ELEVENLABS_DEFAULT_API_KEY_ENV)
            or ""
        ),
        configured_explicitly=(
            isinstance(fields_set, set) and "api_key_env" in fields_set
        ),
        default_env=_ELEVENLABS_DEFAULT_API_KEY_ENV,
        default_base_url=_ELEVENLABS_DEFAULT_BASE_URL,
        effective_base_url=str(
            getattr(provider_config, "base_url", _ELEVENLABS_DEFAULT_BASE_URL)
            or _ELEVENLABS_DEFAULT_BASE_URL
        ),
    )


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _api_url(base_url: str, path: str) -> str:
    if base_url.endswith("/v1") and path.startswith("/v1/"):
        return f"{base_url}{path[3:]}"
    return f"{base_url}{path}"


_FORMAT_MIME_TYPES = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "mp3_22050_32": "audio/mpeg",
    "mp3_44100_128": "audio/mpeg",
    "mp3_44100_192": "audio/mpeg",
    "opus": "audio/opus",
    "pcm": "audio/L16",
    "pcm16": "audio/L16",
    "wav": "audio/wav",
}


def _audio_mime_type(content_type: str | None, response_format: str) -> str:
    mime_type = (content_type or "").split(";", 1)[0].strip()
    if mime_type and mime_type != "application/octet-stream":
        return mime_type
    normalized_format = response_format.lower()
    if normalized_format in _FORMAT_MIME_TYPES:
        return _FORMAT_MIME_TYPES[normalized_format]
    prefix = normalized_format.split("_", 1)[0]
    return _FORMAT_MIME_TYPES.get(prefix, "application/octet-stream")


def _response_error_body(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:1000] if text else "<empty body>"
    return json.dumps(payload, ensure_ascii=False)[:1000]


def _raise_provider_http_error(response: httpx.Response, provider_action: str) -> None:
    if response.is_success:
        return
    body = _response_error_body(response)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"{provider_action} provider returned HTTP "
            f"{response.status_code} {response.reason_phrase}: {body}"
        ) from exc


def _json_field(value: dict[str, str]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass
class AudioGenerationResult:
    audio_bytes: bytes
    provider: str
    model: str
    voice: str | None
    response_format: str
    mime_type: str
    generation_id: str | None = None


@dataclass
class ElevenLabsTextToSpeechRequest:
    text: str
    voice: str
    model_id: str
    output_format: str
    timeout_seconds: float = 120.0
    language_code: str | None = None
    voice_settings: dict[str, Any] | None = None


@dataclass
class ElevenLabsSpeechToTextRequest:
    audio_bytes: bytes
    filename: str
    mime_type: str
    model_id: str = "scribe_v2"
    language_code: str | None = None
    timeout_seconds: float = 120.0


@dataclass
class ElevenLabsSpeechToTextResult:
    text: str
    provider: str
    model: str
    language_code: str | None = None
    language_probability: float | None = None
    words: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class VoiceCloneRequest:
    sample_audio_bytes: bytes
    sample_filename: str
    sample_mime_type: str
    name: str
    description: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 120.0


@dataclass
class VoiceCloneResult:
    provider: str
    voice_id: str
    name: str
    preview_url: str | None = None
    requires_verification: bool = False


@dataclass
class VoiceConversionRequest:
    source_audio_bytes: bytes
    source_filename: str
    source_mime_type: str
    target_voice: str
    model_id: str
    output_format: str = "mp3_44100_128"
    timeout_seconds: float = 120.0


@dataclass
class VoiceConversionResult:
    audio_bytes: bytes
    provider: str
    model: str
    voice: str
    response_format: str
    mime_type: str
    generation_id: str | None = None


@dataclass
class DubbingRequest:
    source_bytes: bytes
    filename: str
    mime_type: str
    target_language: str
    source_language: str | None = None
    name: str | None = None
    num_speakers: int | None = None
    watermark: bool | None = None
    timeout_seconds: float = 120.0


@dataclass
class DubbingResult:
    provider: str
    dubbing_id: str
    status: str
    target_language: str
    source_language: str | None = None


@dataclass
class DubbingStatusRequest:
    dubbing_id: str
    timeout_seconds: float = 120.0


@dataclass
class DubbingStatusResult:
    provider: str
    dubbing_id: str
    status: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DubbingDownloadRequest:
    dubbing_id: str
    language_code: str
    timeout_seconds: float = 120.0


@dataclass
class DubbingDownloadResult:
    audio_bytes: bytes
    provider: str
    dubbing_id: str
    language_code: str
    mime_type: str


@dataclass
class MusicGenerationRequest:
    prompt: str
    model_id: str
    output_format: str
    lyrics: str | None = None
    duration_seconds: float | None = None
    force_instrumental: bool = True
    timeout_seconds: float = 120.0


@dataclass
class MusicGenerationResult:
    audio_bytes: bytes
    provider: str
    model: str
    response_format: str
    mime_type: str
    generation_id: str | None = None


@dataclass
class ElevenLabsSubscriptionRequest:
    timeout_seconds: float = 120.0


@dataclass
class ElevenLabsSubscriptionResult:
    provider: str
    tier: str | None
    status: str | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ElevenLabsVoicesListRequest:
    timeout_seconds: float = 120.0


@dataclass
class ElevenLabsVoicesListResult:
    provider: str
    voices: list[dict[str, Any]]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ElevenLabsSharedVoicesRequest:
    language: str | None = None
    accent: str | None = None
    locale: str | None = None
    gender: str | None = None
    age: str | None = None
    category: str | None = None
    search: str | None = None
    page_size: int = 10
    timeout_seconds: float = 120.0


@dataclass
class ElevenLabsSharedVoicesResult:
    provider: str
    voices: list[dict[str, Any]]
    raw: dict[str, Any] = field(default_factory=dict)


class ElevenLabsAudioProductionProvider:
    provider_id = "elevenlabs"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_env: str = "ELEVENLABS_API_KEY",
        base_url: str = "https://api.elevenlabs.io",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._base_url = _normalize_base_url(base_url)
        self._transport = transport

    def _resolve_api_key(self) -> str:
        return clean_header_secret(
            self._api_key or os.environ.get(self._api_key_env, ""),
            label="ElevenLabs API key",
        )

    def _api_url(self, path: str) -> str:
        return _api_url(self._base_url, path)

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"xi-api-key": api_key}

    async def text_to_speech(
        self, request: ElevenLabsTextToSpeechRequest
    ) -> AudioGenerationResult:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")

        payload: dict[str, Any] = {"text": request.text, "model_id": request.model_id}
        if request.language_code:
            payload["language_code"] = request.language_code
        if request.voice_settings:
            payload["voice_settings"] = request.voice_settings
        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            trust_env=_trust_env(),
            transport=self._transport,
        ) as client:
            response = await client.post(
                self._api_url(f"/v1/text-to-speech/{request.voice}"),
                headers=self._headers(api_key),
                params={"output_format": request.output_format},
                json=payload,
            )
            _raise_provider_http_error(response, "Text to speech")

        audio_bytes = response.content
        if not audio_bytes:
            raise RuntimeError("Text to speech provider returned no audio")
        return AudioGenerationResult(
            audio_bytes=audio_bytes,
            provider=self.provider_id,
            model=request.model_id,
            voice=request.voice,
            response_format=request.output_format,
            mime_type=_audio_mime_type(
                response.headers.get("Content-Type"), request.output_format
            ),
            generation_id=response.headers.get("X-Generation-Id") or None,
        )

    async def transcribe_audio(
        self, request: ElevenLabsSpeechToTextRequest
    ) -> ElevenLabsSpeechToTextResult:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")

        data = {"model_id": request.model_id}
        if request.language_code:
            data["language_code"] = request.language_code
        files = {
            "file": (
                request.filename,
                request.audio_bytes,
                request.mime_type,
            )
        }
        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            trust_env=_trust_env(),
            transport=self._transport,
        ) as client:
            response = await client.post(
                self._api_url("/v1/speech-to-text"),
                headers=self._headers(api_key),
                data=data,
                files=files,
            )
            _raise_provider_http_error(response, "Speech to text")
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("Speech to text provider returned invalid JSON") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("Speech to text provider returned invalid payload")
        text = payload.get("text")
        if not isinstance(text, str):
            raise RuntimeError("Speech to text provider returned no text")
        language_code = payload.get("language_code")
        language_probability = payload.get("language_probability")
        words = payload.get("words")
        return ElevenLabsSpeechToTextResult(
            text=text,
            provider=self.provider_id,
            model=request.model_id,
            language_code=language_code if isinstance(language_code, str) else None,
            language_probability=float(language_probability)
            if isinstance(language_probability, (int, float))
            else None,
            words=[w for w in words if isinstance(w, dict)]
            if isinstance(words, list)
            else [],
            raw=payload,
        )

    async def clone_voice(self, request: VoiceCloneRequest) -> VoiceCloneResult:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")

        data: dict[str, str] = {"name": request.name}
        if request.description:
            data["description"] = request.description
        if request.labels:
            data["labels"] = _json_field(request.labels)
        files = {
            "files": (
                request.sample_filename,
                request.sample_audio_bytes,
                request.sample_mime_type,
            )
        }
        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            trust_env=_trust_env(),
            transport=self._transport,
        ) as client:
            response = await client.post(
                self._api_url("/v1/voices/add"),
                headers=self._headers(api_key),
                data=data,
                files=files,
            )
            _raise_provider_http_error(response, "Voice clone")
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("Voice clone provider returned invalid JSON") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("Voice clone provider returned invalid payload")
        voice_id = payload.get("voice_id")
        if not isinstance(voice_id, str) or not voice_id.strip():
            raise RuntimeError("Voice clone provider returned no voice_id")
        name = payload.get("name")
        preview_url = payload.get("preview_url")
        requires_verification = payload.get("requires_verification")
        return VoiceCloneResult(
            provider=self.provider_id,
            voice_id=voice_id.strip(),
            name=name.strip() if isinstance(name, str) and name.strip() else request.name,
            preview_url=preview_url if isinstance(preview_url, str) else None,
            requires_verification=bool(requires_verification),
        )

    async def convert_voice(
        self, request: VoiceConversionRequest
    ) -> VoiceConversionResult:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")

        files = {
            "audio": (
                request.source_filename,
                request.source_audio_bytes,
                request.source_mime_type,
            )
        }
        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            trust_env=_trust_env(),
            transport=self._transport,
        ) as client:
            response = await client.post(
                self._api_url(f"/v1/speech-to-speech/{request.target_voice}"),
                headers=self._headers(api_key),
                params={"output_format": request.output_format},
                data={"model_id": request.model_id},
                files=files,
            )
            _raise_provider_http_error(response, "Voice conversion")

        audio_bytes = response.content
        if not audio_bytes:
            raise RuntimeError("Voice conversion provider returned no audio")
        return VoiceConversionResult(
            audio_bytes=audio_bytes,
            provider=self.provider_id,
            model=request.model_id,
            voice=request.target_voice,
            response_format=request.output_format,
            mime_type=_audio_mime_type(
                response.headers.get("Content-Type"), request.output_format
            ),
            generation_id=response.headers.get("X-Generation-Id") or None,
        )

    async def create_dubbing(self, request: DubbingRequest) -> DubbingResult:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")

        data: dict[str, str] = {"target_lang": request.target_language}
        if request.source_language:
            data["source_lang"] = request.source_language
        if request.name:
            data["name"] = request.name
        if request.num_speakers is not None:
            data["num_speakers"] = str(request.num_speakers)
        if request.watermark is not None:
            data["watermark"] = str(bool(request.watermark)).lower()
        files = {"file": (request.filename, request.source_bytes, request.mime_type)}
        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            trust_env=_trust_env(),
            transport=self._transport,
        ) as client:
            response = await client.post(
                self._api_url("/v1/dubbing"),
                headers=self._headers(api_key),
                data=data,
                files=files,
            )
            _raise_provider_http_error(response, "Dubbing")
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("Dubbing provider returned invalid JSON") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("Dubbing provider returned invalid payload")
        dubbing_id = payload.get("dubbing_id")
        if not isinstance(dubbing_id, str) or not dubbing_id.strip():
            raise RuntimeError("Dubbing provider returned no dubbing_id")
        status = payload.get("status")
        return DubbingResult(
            provider=self.provider_id,
            dubbing_id=dubbing_id.strip(),
            status=status if isinstance(status, str) and status.strip() else "submitted",
            target_language=request.target_language,
            source_language=request.source_language,
        )

    async def get_dubbing_status(
        self, request: DubbingStatusRequest
    ) -> DubbingStatusResult:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")
        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            trust_env=_trust_env(),
            transport=self._transport,
        ) as client:
            response = await client.get(
                self._api_url(f"/v1/dubbing/{request.dubbing_id}"),
                headers=self._headers(api_key),
            )
            _raise_provider_http_error(response, "Dubbing status")
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("Dubbing status provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Dubbing status provider returned invalid payload")
        status = payload.get("status") or payload.get("state")
        return DubbingStatusResult(
            provider=self.provider_id,
            dubbing_id=request.dubbing_id,
            status=status if isinstance(status, str) and status.strip() else "unknown",
            raw=payload,
        )

    async def download_dubbing_audio(
        self, request: DubbingDownloadRequest
    ) -> DubbingDownloadResult:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")
        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            trust_env=_trust_env(),
            transport=self._transport,
        ) as client:
            response = await client.get(
                self._api_url(
                    f"/v1/dubbing/{request.dubbing_id}/audio/{request.language_code}"
                ),
                headers=self._headers(api_key),
            )
            _raise_provider_http_error(response, "Dubbing download")
        audio_bytes = response.content
        if not audio_bytes:
            raise RuntimeError("Dubbing download provider returned no audio")
        return DubbingDownloadResult(
            audio_bytes=audio_bytes,
            provider=self.provider_id,
            dubbing_id=request.dubbing_id,
            language_code=request.language_code,
            mime_type=_audio_mime_type(response.headers.get("Content-Type"), "mp3"),
        )

    async def generate_music(
        self, request: MusicGenerationRequest
    ) -> MusicGenerationResult:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")

        prompt = request.prompt
        if request.lyrics:
            prompt = f"{prompt}\nLyrics:\n{request.lyrics}"
        payload: dict[str, Any] = {
            "prompt": prompt,
            "model_id": request.model_id,
            "force_instrumental": bool(request.force_instrumental),
        }
        if request.duration_seconds is not None:
            payload["music_length_ms"] = int(request.duration_seconds * 1000)
        if request.lyrics:
            payload["lyrics"] = request.lyrics
        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            trust_env=_trust_env(),
            transport=self._transport,
        ) as client:
            response = await client.post(
                self._api_url("/v1/music"),
                headers=self._headers(api_key),
                params={"output_format": request.output_format},
                json=payload,
            )
            _raise_provider_http_error(response, "Music generation")

        audio_bytes = response.content
        if not audio_bytes:
            raise RuntimeError("Music generation provider returned no audio")
        return MusicGenerationResult(
            audio_bytes=audio_bytes,
            provider=self.provider_id,
            model=request.model_id,
            response_format=request.output_format,
            mime_type=_audio_mime_type(
                response.headers.get("Content-Type"), request.output_format
            ),
            generation_id=response.headers.get("X-Generation-Id") or None,
        )

    async def get_subscription(
        self, request: ElevenLabsSubscriptionRequest
    ) -> ElevenLabsSubscriptionResult:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")
        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            trust_env=_trust_env(),
            transport=self._transport,
        ) as client:
            response = await client.get(
                self._api_url("/v1/user/subscription"),
                headers=self._headers(api_key),
            )
            _raise_provider_http_error(response, "ElevenLabs subscription")
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("Subscription provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Subscription provider returned invalid payload")
        tier = payload.get("tier") or payload.get("plan")
        status = payload.get("status")
        return ElevenLabsSubscriptionResult(
            provider=self.provider_id,
            tier=tier if isinstance(tier, str) else None,
            status=status if isinstance(status, str) else None,
            raw=payload,
        )

    async def list_voices(
        self, request: ElevenLabsVoicesListRequest
    ) -> ElevenLabsVoicesListResult:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")
        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            trust_env=_trust_env(),
            transport=self._transport,
        ) as client:
            response = await client.get(
                self._api_url("/v1/voices"),
                headers=self._headers(api_key),
            )
            _raise_provider_http_error(response, "ElevenLabs voices")
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("Voices provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Voices provider returned invalid payload")
        voices = payload.get("voices")
        return ElevenLabsVoicesListResult(
            provider=self.provider_id,
            voices=[v for v in voices if isinstance(v, dict)]
            if isinstance(voices, list)
            else [],
            raw=payload,
        )

    async def search_shared_voices(
        self, request: ElevenLabsSharedVoicesRequest
    ) -> ElevenLabsSharedVoicesResult:
        api_key = self._resolve_api_key()
        if not api_key:
            raise RuntimeError(f"{self._api_key_env} is not set")
        page_size = max(1, min(int(request.page_size or 10), 50))
        params: dict[str, Any] = {"page_size": page_size}
        for key in ("language", "accent", "locale", "gender", "age", "category", "search"):
            value = getattr(request, key)
            if isinstance(value, str) and value.strip():
                params[key] = value.strip()
        async with httpx.AsyncClient(
            timeout=request.timeout_seconds,
            trust_env=_trust_env(),
            transport=self._transport,
        ) as client:
            response = await client.get(
                self._api_url("/v1/shared-voices"),
                headers=self._headers(api_key),
                params=params,
            )
            _raise_provider_http_error(response, "Shared voices search")
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("Shared voices provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Shared voices provider returned invalid payload")
        voices = payload.get("voices")
        return ElevenLabsSharedVoicesResult(
            provider=self.provider_id,
            voices=[voice for voice in voices if isinstance(voice, dict)]
            if isinstance(voices, list)
            else [],
            raw=payload,
        )
