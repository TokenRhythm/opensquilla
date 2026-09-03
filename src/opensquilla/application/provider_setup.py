"""Application Module for primary-provider setup use cases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from opensquilla.application.setup_mutations import (
    SetupConfigPort,
    SetupMutation,
    SetupRuntimePort,
    commit_setup_mutation,
)


@dataclass(frozen=True, slots=True)
class ConfigurePrimaryProvider:
    provider_id: str
    model: str = ""
    api_key: str = ""
    api_key_env: str = ""
    preserve_api_key: bool = False
    base_url: str = ""
    proxy: str = ""
    preset_id: str = ""
    router_action: str = "preserve"
    image_generation_intent: str = "preserve"


@dataclass(frozen=True, slots=True)
class ProbePrimaryProvider:
    provider_id: str
    model: str = ""
    api_key: str = ""
    api_key_env: str = ""
    base_url: str = ""
    proxy: str = ""
    preserve_api_key: bool = False


@dataclass(frozen=True, slots=True)
class DiscoverPrimaryModels:
    provider_id: str
    api_key: str = ""
    api_key_env: str = ""
    base_url: str = ""
    proxy: str = ""
    force_refresh: bool = False


class ProviderProbePort(Protocol):
    async def probe_primary(self, command: ProbePrimaryProvider) -> Mapping[str, Any]: ...

    async def discover_primary_models(
        self, command: DiscoverPrimaryModels
    ) -> Mapping[str, Any]: ...

    async def discover_image_models(self, provider_id: str) -> Mapping[str, Any]: ...


class PrimaryProviderMutationPort(Protocol):
    def configure_primary(self, config: Any, command: ConfigurePrimaryProvider) -> Any: ...


class ProviderSetup:
    """Configure, probe, and discover the primary provider."""

    def __init__(
        self,
        config: SetupConfigPort,
        runtime: SetupRuntimePort,
        probes: ProviderProbePort,
        mutations: PrimaryProviderMutationPort,
    ) -> None:
        self._config = config
        self._runtime = runtime
        self._probes = probes
        self._mutations = mutations

    async def configure_primary(self, command: ConfigurePrimaryProvider) -> SetupMutation:
        result = self._mutations.configure_primary(
            self._config.active_config(), command
        )
        return await commit_setup_mutation(
            result,
            config_port=self._config,
            effects=(
                self._runtime.sync_primary_provider,
                self._runtime.sync_media,
                self._runtime.refresh_model_catalog,
            ),
        )

    async def probe_primary(self, command: ProbePrimaryProvider) -> dict[str, Any]:
        return dict(await self._probes.probe_primary(command))

    async def discover_primary_models(
        self, command: DiscoverPrimaryModels
    ) -> dict[str, Any]:
        return dict(await self._probes.discover_primary_models(command))

    async def discover_image_models(self, provider_id: str) -> dict[str, Any]:
        return dict(await self._probes.discover_image_models(provider_id))


__all__ = [
    "ConfigurePrimaryProvider",
    "DiscoverPrimaryModels",
    "ProbePrimaryProvider",
    "PrimaryProviderMutationPort",
    "ProviderProbePort",
    "ProviderSetup",
]
