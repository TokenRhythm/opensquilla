"""Application Module for primary-provider setup use cases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NotRequired, Protocol, TypedDict, cast

from opensquilla.application.setup_mutations import (
    SetupConfigPort,
    SetupMutation,
    SetupRuntimePort,
    commit_setup_mutation,
)


class ProviderProbeResult(TypedDict):
    ok: bool
    providerId: str
    model: str
    failureKind: str
    message: str
    code: str
    latencyMs: int
    firstResponseMs: int | None
    totalMs: int


class DiscoveredModelPricing(TypedDict):
    inputPer1k: float
    outputPer1k: float


class DiscoveredModel(TypedDict):
    id: str
    name: str
    contextWindow: int | None
    maxOutputTokens: int | None
    capabilities: list[str]
    pricing: DiscoveredModelPricing | None
    capabilitySource: str
    metadata: NotRequired[dict[str, object] | None]


class ProviderModelDiscoveryResult(TypedDict):
    ok: bool
    failureKind: str
    detail: str
    source: str
    models: list[DiscoveredModel]
    catalog: dict[str, object] | None


class ImageModelDiscoveryResult(TypedDict):
    ok: bool
    providerId: str
    source: str
    models: list[DiscoveredModel]


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
    async def probe_primary(self, command: ProbePrimaryProvider) -> ProviderProbeResult: ...

    async def discover_primary_models(
        self, command: DiscoverPrimaryModels
    ) -> ProviderModelDiscoveryResult: ...

    async def discover_image_models(self, provider_id: str) -> ImageModelDiscoveryResult: ...


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

    async def probe_primary(self, command: ProbePrimaryProvider) -> ProviderProbeResult:
        return cast(ProviderProbeResult, dict(await self._probes.probe_primary(command)))

    async def discover_primary_models(
        self, command: DiscoverPrimaryModels
    ) -> ProviderModelDiscoveryResult:
        return cast(
            ProviderModelDiscoveryResult,
            dict(await self._probes.discover_primary_models(command)),
        )

    async def discover_image_models(self, provider_id: str) -> ImageModelDiscoveryResult:
        return cast(
            ImageModelDiscoveryResult,
            dict(await self._probes.discover_image_models(provider_id)),
        )


__all__ = [
    "ConfigurePrimaryProvider",
    "DiscoveredModel",
    "DiscoveredModelPricing",
    "DiscoverPrimaryModels",
    "ImageModelDiscoveryResult",
    "ProbePrimaryProvider",
    "ProviderModelDiscoveryResult",
    "ProviderProbeResult",
    "PrimaryProviderMutationPort",
    "ProviderProbePort",
    "ProviderSetup",
]
