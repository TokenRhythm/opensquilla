"""Gateway Adapter for setup catalog and status projections."""

from __future__ import annotations

from typing import Any, cast

from opensquilla.application.setup_workflow import SetupCatalog, SetupStatus


def setup_catalog() -> SetupCatalog:
    from opensquilla.onboarding.setup_engine import setup_catalog_payload

    return cast(SetupCatalog, setup_catalog_payload())


def setup_status(config: Any, *, is_owner: bool) -> SetupStatus:
    from opensquilla.onboarding.legacy_data import legacy_data_payload
    from opensquilla.onboarding.mutations import capability_resettable
    from opensquilla.onboarding.next_steps import env_recovery_commands
    from opensquilla.onboarding.probe_history import load_probe_history
    from opensquilla.onboarding.status import get_onboarding_status

    status = get_onboarding_status(
        config,
        probe_history=load_probe_history(config),
    )
    credential = dict(status.llm_credential_status)
    credential["revealAllowed"] = bool(
        is_owner
        and credential.get("available") is True
        and credential.get("source") in {"explicit", "env"}
    )
    config_path = getattr(config, "config_path", None)
    return cast(
        SetupStatus,
        {
            "configPath": str(config_path) if config_path else status.config_path,
            "hasConfig": status.has_config,
            "llmConfigured": status.llm_configured,
            "llmSource": status.llm_source,
            "llmEnvKey": status.llm_env_key,
            "llmCredentialStatus": credential,
            "llmProfileStatus": list(status.llm_profile_status),
            "imageGenerationConfigured": status.image_generation_configured,
            "imageGenerationEnabled": status.image_generation_enabled,
            "imageGenerationSource": status.image_generation_source,
            "imageGenerationProvider": status.image_generation_provider,
            "imageGenerationPrimary": status.image_generation_primary,
            "imageGenerationEnvKey": status.image_generation_env_key,
            "imageGenerationState": status.image_generation_state,
            "audioConfigured": status.audio_configured,
            "audioEnabled": status.audio_enabled,
            "audioSource": status.audio_source,
            "audioProvider": status.audio_provider,
            "audioEnvKey": status.audio_env_key,
            "searchConfigured": status.search_configured,
            "searchProvider": status.search_provider,
            "searchSource": status.search_source,
            "searchEnvKey": status.search_env_key,
            "memoryEmbeddingConfigured": status.memory_embedding_configured,
            "memoryEmbeddingProvider": status.memory_embedding_provider,
            "memoryEmbeddingSource": status.memory_embedding_source,
            "memoryEmbeddingEnvKey": status.memory_embedding_env_key,
            "capabilityConfiguration": {
                capability_id: {
                    "resettable": capability_resettable(
                        config,
                        capability_id=capability_id,
                    )
                }
                for capability_id in (
                    "search",
                    "image_generation",
                    "audio",
                    "memory_embedding",
                )
            },
            "channelCount": status.channel_count,
            "channelsConfigured": status.channels_configured,
            "ensembleCredentialStatus": list(status.ensemble_credential_status),
            "needsOnboarding": status.needs_onboarding,
            "sections": {
                name: section_status.value
                for name, section_status in status.sections.items()
            },
            "sectionDetails": status.section_details,
            "envRecoveryCommands": env_recovery_commands(status),
            "warnings": list(status.warnings),
            "legacyData": legacy_data_payload(),
        },
    )


class GatewaySetupWorkflowPort:
    def __init__(self, config: Any, *, is_owner: bool) -> None:
        self._config = config
        self._is_owner = is_owner

    async def load_setup_catalog(self) -> SetupCatalog:
        return setup_catalog()

    async def load_setup_status(self) -> SetupStatus:
        return setup_status(self._config, is_owner=self._is_owner)


__all__ = [
    "GatewaySetupWorkflowPort",
    "setup_catalog",
    "setup_status",
]
