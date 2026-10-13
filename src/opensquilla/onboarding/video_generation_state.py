"""Shared video configuration status for setup surfaces."""

from __future__ import annotations

from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.video_generation_catalog import (
    list_video_generation_provider_catalog_entries,
)
from opensquilla.provider.video_generation_credentials import (
    video_generation_credential_status,
)


def resolve_video_generation_state(config: GatewayConfig) -> dict[str, object]:
    video = config.video_generation
    return {
        "enabled": video.enabled,
        "providerId": video.effective_provider,
        "primary": video.primary,
        "credentialOptions": [
            video_generation_credential_status(
                config,
                provider_id=spec.provider_id,
            )
            for spec in list_video_generation_provider_catalog_entries()
        ],
    }


__all__ = ["resolve_video_generation_state"]
