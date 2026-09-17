"""Application Module for setup status and catalog use cases.

Setup mutations are intentionally not collapsed into a generic ``configure``
or ``reset`` operation.  Their domain Implementations remain the explicit
functions in :mod:`opensquilla.onboarding.mutations`; the Gateway owns their
persistence and live-runtime reconciliation.  This Module provides the shared
read seam used by WebUI setup consumers without importing ``RpcContext``.
"""

from __future__ import annotations

from typing import Protocol, TypedDict, cast


class SetupField(TypedDict, total=False):
    name: str
    label: str
    type: str
    required: bool
    default: object
    choices: list[str]
    description: str
    secret: bool
    group: str
    advanced: bool
    showWhen: dict[str, object]
    help: str
    placeholder: str


class SetupCatalogEntry(TypedDict, total=False):
    providerId: str
    type: str
    label: str
    description: str
    backend: str
    providerKind: str
    runtimeSupported: bool
    metadataSupported: bool
    verification: str
    envKey: str
    defaultBaseUrl: str
    acceptsApiKey: bool
    requiresApiKey: bool
    requiresBaseUrl: bool
    routerSupported: bool
    deployment: str
    transport: str
    blocking: bool
    canProbe: bool
    requiresPublicUrl: bool
    dependencyExtra: str
    restartRequired: bool
    docsHint: str
    help: str
    readmeScenarios: list[str]
    whatYouNeed: list[str]
    capabilities: list[str]
    fields: list[SetupField]
    presets: list[dict[str, object]]
    suggestedModels: list[str]
    defaultDirectModel: str
    defaultModel: str
    defaultTtsModel: str
    defaultTtsVoice: str
    defaultLanguageCode: str
    setupAids: list[dict[str, str]]


class RouterCatalogMode(TypedDict):
    mode: str
    label: str
    description: str


class RouterCatalogProfile(TypedDict):
    profileId: str
    providerId: str
    label: str
    tiers: dict[str, dict[str, object]]


class RouterCatalog(TypedDict):
    defaultTier: str
    textTiers: list[str]
    modes: list[RouterCatalogMode]
    profiles: list[RouterCatalogProfile]


class SetupCatalog(TypedDict):
    providers: list[SetupCatalogEntry]
    routerProfiles: RouterCatalog
    searchProviders: list[SetupCatalogEntry]
    channels: list[SetupCatalogEntry]
    imageGenerationProviders: list[SetupCatalogEntry]
    audioProviders: list[SetupCatalogEntry]
    memoryEmbeddingProviders: list[SetupCatalogEntry]


class SetupCredentialStatus(TypedDict, total=False):
    provider: str
    available: bool
    source: str
    envKey: str
    masked: str
    revealAllowed: bool


class SetupProfileStatus(TypedDict, total=False):
    provider: str
    ready: bool
    credentialSource: str
    credentialEnv: str
    endpointSource: str
    proxySource: str
    reason: str
    primaryEligible: bool
    primaryBlockReason: str
    lastProbe: dict[str, object]


class SetupSectionDetail(TypedDict, total=False):
    label: str
    status: str
    required: bool
    optional: bool
    blocking: bool
    actionRequired: bool
    detail: str
    routerMode: str
    routerBinding: str
    routerProviderConflicts: list[str]
    routerProviderRoles: dict[str, object]
    tierEnsembleStatuses: list[dict[str, object]]
    tierEnsembleStatus: dict[str, object]
    providerResolution: dict[str, object]


class CapabilityConfiguration(TypedDict):
    resettable: bool


class SetupStatus(TypedDict):
    configPath: str | None
    hasConfig: bool
    llmConfigured: bool
    llmSource: str
    llmEnvKey: str
    llmCredentialStatus: SetupCredentialStatus
    llmProfileStatus: list[SetupProfileStatus]
    imageGenerationConfigured: bool
    imageGenerationEnabled: bool
    imageGenerationSource: str
    imageGenerationProvider: str
    imageGenerationPrimary: str
    imageGenerationEnvKey: str
    imageGenerationState: dict[str, object]
    audioConfigured: bool
    audioEnabled: bool
    audioSource: str
    audioProvider: str
    audioEnvKey: str
    searchConfigured: bool
    searchProvider: str
    searchSource: str
    searchEnvKey: str
    memoryEmbeddingConfigured: bool
    memoryEmbeddingProvider: str
    memoryEmbeddingSource: str
    memoryEmbeddingEnvKey: str
    capabilityConfiguration: dict[str, CapabilityConfiguration]
    channelCount: int
    channelsConfigured: bool
    ensembleCredentialStatus: list[SetupCredentialStatus]
    needsOnboarding: bool
    sections: dict[str, str]
    sectionDetails: dict[str, SetupSectionDetail]
    envRecoveryCommands: list[str]
    warnings: list[str]
    legacyData: None


class SetupCatalogPort(Protocol):
    async def load_setup_catalog(self) -> SetupCatalog: ...


class SetupStatusPort(Protocol):
    async def load_setup_status(self) -> SetupStatus: ...


class SetupWorkflow:
    """Expose real setup read use cases over explicit Ports."""

    def __init__(self, catalog: SetupCatalogPort, status: SetupStatusPort) -> None:
        self._catalog = catalog
        self._status = status

    async def catalog(self) -> SetupCatalog:
        return cast(SetupCatalog, dict(await self._catalog.load_setup_catalog()))

    async def status(self) -> SetupStatus:
        return cast(SetupStatus, dict(await self._status.load_setup_status()))
