"""Onboarding-friendly audio provider catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

FieldType = Literal["text", "password", "select", "bool"]


@dataclass(frozen=True)
class AudioProviderSetupField:
    name: str
    label: str
    field_type: FieldType
    required: bool
    default: str | bool | None = None
    choices: tuple[str, ...] = ()
    description: str = ""
    secret: bool = False


@dataclass(frozen=True)
class AudioProviderSetupSpec:
    provider_id: str
    label: str
    runtime_supported: bool
    requires_api_key: bool
    env_key: str
    default_base_url: str
    default_tts_model: str
    default_tts_voice: str
    default_language_code: str
    deployment: str
    blocking: bool
    can_probe: bool
    readme_scenarios: tuple[str, ...]
    what_you_need: tuple[str, ...]
    fields: tuple[AudioProviderSetupField, ...]


_AUDIO_PROVIDER_DATA: dict[str, dict[str, Any]] = {
    "elevenlabs": {
        "label": "ElevenLabs Audio",
        "env_key": "ELEVENLABS_API_KEY",
        "default_base_url": "https://api.elevenlabs.io",
        "default_tts_model": "eleven_multilingual_v2",
        "default_tts_voice": "21m00Tcm4TlvDq8ikWAM",
        "default_language_code": "",
    },
}


def _fields_for(data: dict[str, Any]) -> tuple[AudioProviderSetupField, ...]:
    return (
        AudioProviderSetupField(
            name="enabled",
            label="Enabled",
            field_type="bool",
            required=False,
            default=True,
        ),
        AudioProviderSetupField(
            name="api_key",
            label="API key",
            field_type="password",
            required=False,
            default="",
            description=f"May be provided by {data['env_key']}.",
            secret=True,
        ),
        AudioProviderSetupField(
            name="api_key_env",
            label="API key env",
            field_type="text",
            required=False,
            default=str(data["env_key"]),
            description="Environment variable to read for audio provider access.",
        ),
        AudioProviderSetupField(
            name="base_url",
            label="Base URL",
            field_type="text",
            required=False,
            default=str(data["default_base_url"]),
            description="Override the upstream HTTP base URL.",
        ),
        AudioProviderSetupField(
            name="tts_voice",
            label="TTS voice",
            field_type="text",
            required=False,
            default=str(data["default_tts_voice"]),
            description="Default voice id for text-to-speech.",
        ),
        AudioProviderSetupField(
            name="tts_model",
            label="TTS model",
            field_type="text",
            required=False,
            default=str(data["default_tts_model"]),
            description="Default model for text-to-speech.",
        ),
        AudioProviderSetupField(
            name="language_code",
            label="Language code",
            field_type="text",
            required=False,
            default=str(data["default_language_code"]),
            description="Optional locale hint such as zh-CN, en-US, or en-GB.",
        ),
    )


def list_audio_provider_setup_specs() -> list[AudioProviderSetupSpec]:
    return [
        AudioProviderSetupSpec(
            provider_id=provider_id,
            label=str(data["label"]),
            runtime_supported=True,
            requires_api_key=True,
            env_key=str(data["env_key"]),
            default_base_url=str(data["default_base_url"]),
            default_tts_model=str(data["default_tts_model"]),
            default_tts_voice=str(data["default_tts_voice"]),
            default_language_code=str(data["default_language_code"]),
            deployment="cloud",
            blocking=False,
            can_probe=False,
            readme_scenarios=(
                "text-to-speech",
                "speech-to-text",
                "voice cloning",
                "voice conversion",
                "dubbing",
                "music and singing",
            ),
            what_you_need=(
                f"API key via {data['env_key']} or a one-time paste.",
                "A default TTS voice id for generated speech.",
                "Optional language code to guide accent and pronunciation.",
            ),
            fields=_fields_for(data),
        )
        for provider_id, data in _AUDIO_PROVIDER_DATA.items()
    ]


def get_audio_provider_setup_spec(provider_id: str) -> AudioProviderSetupSpec:
    for spec in list_audio_provider_setup_specs():
        if spec.provider_id == provider_id:
            return spec
    raise KeyError(f"unknown audio provider: {provider_id!r}")


def audio_provider_catalog_payload() -> list[dict[str, Any]]:
    return [
        {
            "providerId": s.provider_id,
            "label": s.label,
            "runtimeSupported": s.runtime_supported,
            "requiresApiKey": s.requires_api_key,
            "envKey": s.env_key,
            "defaultBaseUrl": s.default_base_url,
            "defaultTtsModel": s.default_tts_model,
            "defaultTtsVoice": s.default_tts_voice,
            "defaultLanguageCode": s.default_language_code,
            "deployment": s.deployment,
            "blocking": s.blocking,
            "canProbe": s.can_probe,
            "readmeScenarios": list(s.readme_scenarios),
            "whatYouNeed": list(s.what_you_need),
            "fields": [
                {
                    "name": f.name,
                    "label": f.label,
                    "type": f.field_type,
                    "required": f.required,
                    "default": f.default,
                    "choices": list(f.choices),
                    "description": f.description,
                    "secret": f.secret,
                }
                for f in s.fields
            ],
        }
        for s in list_audio_provider_setup_specs()
    ]
