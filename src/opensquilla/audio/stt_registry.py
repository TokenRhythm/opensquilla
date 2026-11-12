"""Build the configured STT provider chain.

The chain is one primary plus optional fallbacks, tried in order by the
route/tool caller.  Provider names and settings live under ``[audio.stt]``;
``elevenlabs`` remains the default so existing deployments keep identical
behaviour until an operator opts in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opensquilla.audio.stt_elevenlabs import ElevenLabsSttProvider
from opensquilla.audio.stt_openai import (
    OpenAIChatInputAudioSttProvider,
    OpenAITranscriptionsSttProvider,
)
from opensquilla.audio.stt_types import SttProvider
from opensquilla.endpoint_identity import credential_env_for_endpoint

if TYPE_CHECKING:
    from opensquilla.gateway.config import GatewayConfig

_OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_OPENAI_DEFAULT_KEY_ENV = "OPENAI_API_KEY"


class SttConfigError(ValueError):
    """Raised when ``[audio.stt]`` names an unknown or incomplete provider."""


def resolve_stt_api_key_env(provider_config: object) -> str:
    """Resolve an STT key env reference without leaking it across origins."""

    fields_set = getattr(provider_config, "model_fields_set", None)
    return credential_env_for_endpoint(
        configured_env=str(getattr(provider_config, "api_key_env", "") or ""),
        configured_explicitly=(
            isinstance(fields_set, set) and "api_key_env" in fields_set
        ),
        default_env=_OPENAI_DEFAULT_KEY_ENV,
        default_base_url=_OPENAI_DEFAULT_BASE_URL,
        effective_base_url=str(
            getattr(provider_config, "base_url", "") or _OPENAI_DEFAULT_BASE_URL
        ),
    )


def _build_openai(provider_config: object, *, chat_style: bool) -> SttProvider:
    timeout = float(getattr(provider_config, "timeout_seconds", 120.0) or 120.0)
    cls = OpenAIChatInputAudioSttProvider if chat_style else OpenAITranscriptionsSttProvider
    return cls(
        api_key=str(getattr(provider_config, "api_key", "") or ""),
        api_key_env=resolve_stt_api_key_env(provider_config),
        base_url=str(getattr(provider_config, "base_url", "") or _OPENAI_DEFAULT_BASE_URL),
        model=str(getattr(provider_config, "model", "") or ""),
        timeout_seconds=timeout,
    )


def _build_elevenlabs(config: GatewayConfig) -> SttProvider:
    from opensquilla.provider.audio import resolve_elevenlabs_api_key_env

    provider_cfg = config.audio.providers.elevenlabs
    return ElevenLabsSttProvider(
        api_key=getattr(provider_cfg, "api_key", ""),
        api_key_env=resolve_elevenlabs_api_key_env(provider_cfg),
        base_url=getattr(provider_cfg, "base_url", "https://api.elevenlabs.io"),
        default_model=getattr(provider_cfg, "speech_to_text_model", "scribe_v2"),
    )


def _build_one(name: str, config: GatewayConfig) -> SttProvider:
    normalized = str(name or "").strip().lower()
    if normalized == "elevenlabs":
        return _build_elevenlabs(config)
    stt_cfg = config.audio.stt
    if normalized == "openai_transcriptions":
        return _build_openai(stt_cfg.openai_transcriptions, chat_style=False)
    if normalized == "openai_chat":
        return _build_openai(stt_cfg.openai_chat, chat_style=True)
    known = "elevenlabs | openai_transcriptions | openai_chat"
    raise SttConfigError(
        f"unknown [audio.stt] provider {name!r}; expected one of: {known} "
        "(local_sensevoice arrives with the optional voice extra)"
    )


def build_stt_chain(config: GatewayConfig) -> list[SttProvider]:
    """Return the ordered providers for one transcription request."""

    stt_cfg = config.audio.stt
    primary_name = str(getattr(stt_cfg, "provider", "") or "").strip() or "elevenlabs"
    names: list[str] = [primary_name]
    for extra in list(getattr(stt_cfg, "fallback", None) or []):
        normalized = str(extra or "").strip().lower()
        if normalized and normalized not in [n.lower() for n in names]:
            names.append(normalized)
    return [_build_one(name, config) for name in names]
