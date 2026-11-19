"""Loopback TTS synthesis for local companions (e.g. the Eggy desktop pet).

Registers ``POST /api/audio/speech`` so a trusted local client can reuse the
operator's configured speech settings (``[audio.providers.*]`` credentials,
``[audio.tts]`` voice/model/preset, or an OpenAI-compatible ``[audio.stt]``
endpoint) without the API key ever leaving the gateway. The companion sends
text and receives audio bytes.

Provider selection mirrors the ``tts`` tool: ElevenLabs when configured,
otherwise an OpenAI-compatible ``/audio/speech`` endpoint reusing the STT
platform credentials (the same URL/key the operator filled for recognition).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.origin_guard import (
    forbidden_origin_response,
    request_origin_allowed,
)
from opensquilla.gateway.uploads import _authorization_token_matches

_MAX_TEXT = 4096


def _openai_audio_endpoint(config: Any) -> tuple[str, str] | None:
    """Return (base_url, api_key) of an OpenAI-compatible audio credential."""
    stt = getattr(config, "stt", None)
    for name in ("openai_transcriptions", "openai_chat"):
        provider = getattr(stt, name, None)
        if provider is None:
            continue
        base_url = str(getattr(provider, "base_url", "") or "").strip()
        api_key = str(getattr(provider, "api_key", "") or "").strip()
        if not api_key:
            api_key = str(
                os.environ.get(str(getattr(provider, "api_key_env", "") or ""), "")
            ).strip()
        if base_url and api_key:
            return base_url.rstrip("/"), api_key
    return None


def register_audio_speech_routes(app: Any, *, config: GatewayConfig) -> None:
    """Register POST /api/audio/speech (text -> audio bytes)."""

    async def speech_handler(request: Request) -> Response:
        if not request_origin_allowed(request, config):
            return forbidden_origin_response()
        if config.auth.mode == "token":
            if config.auth.token and not _authorization_token_matches(config, request):
                return JSONResponse(
                    {
                        "error": "Authorization required for /api/audio/speech",
                        "code": "UNAUTHORIZED",
                    },
                    status_code=401,
                )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "text is required"}, status_code=400)
        if len(text) > _MAX_TEXT:
            return JSONResponse({"error": f"text exceeds {_MAX_TEXT} characters"}, status_code=400)

        from opensquilla.tools.builtin import media as _media

        audio_config = _media._resolve_audio_config()
        voice = str(body.get("voice") or "").strip()
        preset = str(body.get("preset") or "").strip()
        model = str(body.get("model") or "").strip()

        elevenlabs_response: Response | None = None
        if _media._audio_configured(audio_config):
            elevenlabs_response = await _elevenlabs_speech(
                _media, audio_config, text, voice, preset, model
            )
            if elevenlabs_response.status_code == 200:
                return elevenlabs_response
        # Fall back to the OpenAI-compatible platform credentials (the same
        # URL/key the operator filled for recognition) when ElevenLabs is
        # absent or rejects the request.
        endpoint = _openai_audio_endpoint(audio_config)
        if endpoint is not None:
            openai_response = await _openai_speech(endpoint, audio_config, text, voice, model)
            if openai_response.status_code == 200:
                return openai_response
            return openai_response
        if elevenlabs_response is not None:
            return elevenlabs_response
        return JSONResponse(
            {"error": "no speech provider configured", "code": "UNAVAILABLE"},
            status_code=503,
        )

    app.router.routes.append(Route("/api/audio/speech", speech_handler, methods=["POST"]))


async def _elevenlabs_speech(
    media: Any, audio_config: Any, text: str, voice: str, preset: str, model: str
) -> Response:
    from opensquilla.audio.voice_presets import VoicePresetError, resolve_voice
    from opensquilla.provider.audio import ElevenLabsTextToSpeechRequest

    tts_config = getattr(audio_config, "tts", None)
    resolved_preset = None
    preset_name = preset or str(getattr(tts_config, "preset", "") or "").strip()
    if preset_name:
        try:
            # resolve_voice expects the full config surface (config.audio.*).
            resolved_preset = resolve_voice(
                SimpleNamespace(audio=audio_config),  # type: ignore[arg-type]
                preset_name,
            )
        except VoicePresetError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    model_id = model or (resolved_preset.model_id if resolved_preset else "") or str(
        getattr(tts_config, "model", "") or "eleven_multilingual_v2"
    )
    resolved_voice = voice or (resolved_preset.voice_id if resolved_preset else "") or str(
        getattr(tts_config, "voice", "") or ""
    )
    if not resolved_voice:
        return JSONResponse({"error": "voice is not configured"}, status_code=503)
    provider = media._elevenlabs_provider(audio_config)
    try:
        result = await provider.text_to_speech(
            ElevenLabsTextToSpeechRequest(
                text=text,
                voice=resolved_voice,
                model_id=model_id,
                output_format=str(getattr(tts_config, "output_format", "") or "mp3_44100_128"),
                timeout_seconds=float(getattr(tts_config, "timeout_seconds", 120.0) or 120.0),
            )
        )
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc), "code": "UNAVAILABLE"}, status_code=503)
    return Response(
        content=result.audio_bytes,
        media_type=result.mime_type or "audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


async def _openai_speech(
    endpoint: tuple[str, str], audio_config: Any, text: str, voice: str, model: str
) -> Response:
    base_url, api_key = endpoint
    tts_config = getattr(audio_config, "tts", None)
    payload = {
        "model": model or str(getattr(tts_config, "model", "") or "") or "tts-1",
        "input": text,
        "voice": voice or str(getattr(tts_config, "voice", "") or "") or "alloy",
        "response_format": "mp3",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            response = await client.post(
                base_url + "/audio/speech",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        return JSONResponse(
            {"error": f"speech request failed: {exc}", "code": "UNAVAILABLE"},
            status_code=503,
        )
    if response.status_code != 200:
        return JSONResponse(
            {"error": f"speech provider HTTP {response.status_code}", "code": "UNAVAILABLE"},
            status_code=503,
        )
    return Response(
        content=response.content,
        media_type=response.headers.get("content-type") or "audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["register_audio_speech_routes"]
