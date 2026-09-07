"""Gateway adapters for the shared audio capability Application Module."""

from __future__ import annotations

from typing import Any, cast

from opensquilla.application.capability_setup import (
    AudioCapabilitySetup,
    ConfigureAudio,
)
from opensquilla.gateway.adapters.setup_config import GatewaySetupConfigPort
from opensquilla.gateway.adapters.setup_mutations import OnboardingSetupMutationPort
from opensquilla.gateway.setup_config_runtime import sync_media_runtime


class GatewayAudioRuntimePort:
    async def sync_media(self, config: Any) -> None:
        sync_media_runtime(config)


def _application(holder: Any) -> AudioCapabilitySetup:
    return AudioCapabilitySetup(
        GatewaySetupConfigPort(holder),
        GatewayAudioRuntimePort(),
        OnboardingSetupMutationPort(),
    )


async def configure_audio_provider(holder: Any, command: ConfigureAudio) -> dict[str, Any]:
    """Configure one operator-specified audio provider and return a safe payload."""

    return cast(
        dict[str, Any],
        (await _application(holder).configure(command)).to_payload(),
    )


async def configure_agent_audio_provider(
    holder: Any,
    *,
    provider_id: str,
    api_key: str = "",
    api_key_env: str = "",
    enabled: bool = True,
    tts_voice: str = "",
    tts_model: str = "",
    language_code: str = "",
) -> dict[str, Any]:
    """Apply the constrained registry-owned audio configuration for agents."""

    from opensquilla.onboarding.audio_specs import get_audio_provider_setup_spec

    spec = get_audio_provider_setup_spec(provider_id)
    if api_key_env and api_key_env != spec.env_key:
        raise ValueError(
            f"audio provider {provider_id!r} only accepts api_key_env={spec.env_key!r} "
            "through this tool"
        )
    return await configure_audio_provider(
        holder,
        ConfigureAudio(
            provider_id=provider_id,
            api_key=api_key,
            api_key_env=api_key_env,
            base_url=spec.default_base_url,
            enabled=enabled,
            tts_voice=tts_voice,
            tts_model=tts_model,
            language_code=language_code,
        ),
    )


__all__ = ["configure_agent_audio_provider", "configure_audio_provider"]
