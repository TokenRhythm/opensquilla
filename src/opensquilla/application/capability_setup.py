"""Application Module for setup capability configuration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from opensquilla.application.setup_mutations import (
    SetupConfigPort,
    SetupMutation,
    SetupRuntimePort,
    commit_setup_mutation,
)


@dataclass(frozen=True, slots=True)
class ConfigureRouter:
    mode: str = "recommended"
    default_tier: str | None = None
    tiers: Mapping[str, Any] | None = None
    cross_provider_tiers: bool | None = None
    tier_provider_mismatch: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigureEnsemble:
    enabled: bool | None = None
    selection_mode: str | None = None
    model_options: Sequence[Any] | None = None
    candidates: Sequence[Any] | None = None
    min_successful_proposers: int | None = None
    proposer_max_retries: int | None = None
    all_failed_policy: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigureSearch:
    provider_id: str
    api_key: str = ""
    api_key_env: str = ""
    max_results: int | str = 5
    proxy: str = ""
    use_env_proxy: bool = False
    fallback_policy: str = "off"
    diagnostics: bool = False


@dataclass(frozen=True, slots=True)
class ConfigureImageGeneration:
    provider_id: str
    primary: str = ""
    api_key: str = ""
    api_key_env: str = ""
    base_url: str | None = None
    enabled: bool = True
    size: str = ""
    output_format: str = ""
    fallbacks: Sequence[Any] | None = None
    clear_fallbacks: bool = False
    credential_mode: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigureMemoryEmbedding:
    provider_id: str
    model: str = ""
    api_key: str = ""
    api_key_env: str = ""
    base_url: str = ""
    onnx_dir: str = ""


@dataclass(frozen=True, slots=True)
class ConfigureAudio:
    provider_id: str
    api_key: str = ""
    api_key_env: str = ""
    base_url: str = ""
    enabled: bool = True
    tts_voice: str = ""
    tts_model: str = ""
    language_code: str = ""


class CapabilityMutationPort(Protocol):
    def configure_router(self, config: Any, command: ConfigureRouter) -> Any: ...

    def configure_ensemble(self, config: Any, command: ConfigureEnsemble) -> Any: ...

    def configure_search(self, config: Any, command: ConfigureSearch) -> Any: ...

    def configure_image_generation(
        self, config: Any, command: ConfigureImageGeneration
    ) -> Any: ...

    def configure_memory_embedding(
        self, config: Any, command: ConfigureMemoryEmbedding
    ) -> Any: ...

    def configure_audio(self, config: Any, command: ConfigureAudio) -> Any: ...

    def reset(self, config: Any, capability_id: str) -> Any: ...


class CapabilitySetup:
    def __init__(
        self,
        config: SetupConfigPort,
        runtime: SetupRuntimePort,
        mutations: CapabilityMutationPort,
    ) -> None:
        self._config = config
        self._runtime = runtime
        self._mutations = mutations

    async def configure_router(self, command: ConfigureRouter) -> SetupMutation:
        result = self._mutations.configure_router(
            self._config.active_config(), command
        )

        async def reconcile(config: Any) -> None:
            await self._runtime.sync_primary_provider(config)
            await self._runtime.broadcast_model_routing(
                config, source="onboarding.router.configure"
            )

        return await commit_setup_mutation(
            result, config_port=self._config, effects=(reconcile,)
        )

    async def configure_ensemble(self, command: ConfigureEnsemble) -> SetupMutation:
        result = self._mutations.configure_ensemble(
            self._config.active_config(), command
        )

        async def reconcile(config: Any) -> None:
            await self._runtime.broadcast_model_routing(
                config, source="onboarding.ensemble.configure"
            )

        return await commit_setup_mutation(
            result, config_port=self._config, effects=(reconcile,)
        )

    async def configure_search(self, command: ConfigureSearch) -> SetupMutation:
        result = self._mutations.configure_search(
            self._config.active_config(), command
        )
        return await commit_setup_mutation(
            result,
            config_port=self._config,
            effects=(self._runtime.sync_search,),
        )

    async def configure_image_generation(
        self, command: ConfigureImageGeneration
    ) -> SetupMutation:
        result = self._mutations.configure_image_generation(
            self._config.active_config(), command
        )
        return await commit_setup_mutation(
            result, config_port=self._config, effects=(self._runtime.sync_media,)
        )

    async def configure_memory_embedding(
        self, command: ConfigureMemoryEmbedding
    ) -> SetupMutation:
        result = self._mutations.configure_memory_embedding(
            self._config.active_config(), command
        )
        return await commit_setup_mutation(result, config_port=self._config)

    async def configure_audio(self, command: ConfigureAudio) -> SetupMutation:
        result = self._mutations.configure_audio(
            self._config.active_config(), command
        )
        return await commit_setup_mutation(
            result, config_port=self._config, effects=(self._runtime.sync_media,)
        )

    async def reset(self, capability_id: str) -> SetupMutation:
        result = self._mutations.reset(self._config.active_config(), capability_id)
        config_path = self._config.persist_candidate(
            result.config,
            restart_required=bool(result.restart_required),
            remove_paths=result.remove_paths,
        )
        live = self._config.install_candidate(result.config)
        restart_required = bool(result.restart_required)
        warnings = [str(item) for item in result.warnings]
        canonical = str(result.public_payload["capabilityId"])
        try:
            if canonical == "search":
                await self._runtime.sync_search(live)
            elif canonical in {"image_generation", "audio"}:
                await self._runtime.sync_media(live)
        except Exception:  # noqa: BLE001 - durable reset degrades to restart
            restart_required = True
            warnings.append(
                "Capability reset was saved, but the live runtime could not be "
                "updated. Restart the gateway to apply it."
            )
        return SetupMutation(
            changed=bool(result.changed),
            restart_required=restart_required,
            config_path=config_path,
            entry=dict(result.public_payload),
            warnings=tuple(warnings),
        )


__all__ = [
    "CapabilitySetup",
    "CapabilityMutationPort",
    "ConfigureAudio",
    "ConfigureEnsemble",
    "ConfigureImageGeneration",
    "ConfigureMemoryEmbedding",
    "ConfigureRouter",
    "ConfigureSearch",
]
