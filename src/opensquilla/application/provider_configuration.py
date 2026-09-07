"""Application Modules for provider, model-catalog and routing use cases."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, Protocol, TypedDict, cast

from opensquilla.application.setup_mutations import SetupConfigPort


class ModelPricing(TypedDict):
    inputPer1k: float
    outputPer1k: float


class ModelDescriptor(TypedDict):
    id: str
    name: str
    provider: str
    contextWindow: int
    maxOutputTokens: int
    capabilities: list[str]
    pricing: ModelPricing
    source: str
    reasoningFormat: str
    metadata: dict[str, object] | None


class ModelCatalogError(TypedDict):
    provider: str
    kind: str
    detail: str


class ModelCatalogResult(TypedDict):
    models: list[ModelDescriptor]
    errors: list[ModelCatalogError]


type ModelRoutingMode = Literal["direct", "router", "ensemble"]


class ImageInputRouting(TypedDict):
    admission: str
    reason: str


class ModelRoutingCapabilities(TypedDict):
    image_input: ImageInputRouting


class EnsembleActivationPreview(TypedDict):
    selection_mode: str
    proposer_count: int
    member_providers: list[str]
    candidates: list[dict[str, object]]
    blocked_reason: str | None
    selection_configured: NotRequired[bool]


class ModelRoutingSnapshot(TypedDict):
    mode: ModelRoutingMode
    router_enabled: bool
    ensemble_enabled: bool
    rollout_phase: str
    selection_mode: str
    selection_configured: bool
    activation_preview: EnsembleActivationPreview
    router_required_by_ensemble: bool
    image_input: ImageInputRouting
    applies_to: str
    capabilities_by_mode: dict[str, ModelRoutingCapabilities]


class ModelRoutingMutation(ModelRoutingSnapshot):
    patched: list[str]
    restart_required: bool
    restartRequired: NotRequired[bool]


class ProviderModelProbe(TypedDict):
    attempted: bool
    status: str
    count: int
    error: str | None
    failureKind: str | None


class ProviderLatency(TypedDict):
    p50TtftMs: int | None
    p95TtftMs: int | None
    samples: int
    windowMinutes: int


class ProviderProjection(TypedDict):
    providerId: str
    active: bool
    configured: bool
    buildable: bool
    model: str
    requiresApiKey: bool
    apiKeyEnv: str
    apiKeyConfigured: bool
    apiKeyShape: str
    baseUrlConfigured: bool
    error: str | None
    modelProbe: ProviderModelProbe
    latency: ProviderLatency | None


class ProviderResolution(TypedDict):
    status: str
    effectiveProvider: str
    source: str
    reasonCode: str
    actionRequired: bool
    actionRecommended: bool


class ProviderStatusResult(TypedDict):
    activeProvider: str | None
    providerResolution: ProviderResolution
    providers: list[ProviderProjection]
    count: int


class ModelCatalogPort(Protocol):
    async def load_model_catalog(self) -> ModelCatalogResult: ...


@dataclass(frozen=True, slots=True)
class PreparedModelRouting:
    config: Any
    patched: tuple[str, ...]


class ModelRoutingPolicyPort(Protocol):
    def snapshot(self, config: Any) -> ModelRoutingSnapshot: ...

    def prepare(self, config: Any, mode: str) -> PreparedModelRouting: ...


class ModelRoutingRuntimePort(Protocol):
    def prepare_reconciliation(self, config: Any) -> Any: ...

    async def reconcile(self, config: Any, prepared: Any) -> None: ...

    async def publish_changed(
        self,
        previous: ModelRoutingSnapshot,
        config: Any,
        *,
        source: str,
    ) -> None: ...


class ProviderStatusPort(Protocol):
    async def load_provider_status(
        self,
        *,
        provider_id: str | None,
        probe_models: bool,
    ) -> ProviderStatusResult: ...


class ModelCatalog:
    """Query and filter the public model catalog without wire knowledge."""

    def __init__(self, port: ModelCatalogPort) -> None:
        self._port = port

    async def query(
        self,
        *,
        provider_id: str | None = None,
        capabilities: Sequence[str] | None = None,
    ) -> ModelCatalogResult:
        result = await self._port.load_model_catalog()
        models = [
            cast(ModelDescriptor, dict(row))
            for row in result.get("models", ())
            if isinstance(row, Mapping)
        ]
        errors = [
            cast(ModelCatalogError, dict(row))
            for row in result.get("errors", ())
            if isinstance(row, Mapping)
        ]
        provider = str(provider_id or "").strip()
        if provider:
            models = [row for row in models if row.get("provider") == provider]
        required = {str(item).strip() for item in capabilities or () if str(item).strip()}
        if required:
            models = [
                row
                for row in models
                if required.issubset(
                    {str(item) for item in row.get("capabilities", ())}
                )
            ]
        return ModelCatalogResult(models=models, errors=errors)


class ModelRouting:
    """Read and durably change the operator's routing intent.

    Candidate creation, persistence, live installation, runtime reconciliation,
    and event publication are one ordered Application transaction.  Gateway
    request objects and wire fields stay behind the injected Ports.
    """

    def __init__(
        self,
        config: SetupConfigPort,
        policy: ModelRoutingPolicyPort,
        runtime: ModelRoutingRuntimePort,
    ) -> None:
        self._config = config
        self._policy = policy
        self._runtime = runtime

    async def read(self) -> ModelRoutingSnapshot:
        return cast(
            ModelRoutingSnapshot,
            dict(self._policy.snapshot(self._config.active_config())),
        )

    async def set_mode(self, mode: str) -> ModelRoutingMutation:
        normalized = str(mode or "").strip()
        if not normalized:
            raise ValueError("routing mode is required")
        current = self._config.active_config()
        previous = self._policy.snapshot(current)
        candidate = self._policy.prepare(current, normalized)
        prepared_runtime = self._runtime.prepare_reconciliation(candidate.config)
        self._config.persist_candidate(candidate.config, restart_required=False)
        live = self._config.install_candidate(candidate.config)
        await self._runtime.reconcile(live, prepared_runtime)
        await self._runtime.publish_changed(
            previous,
            live,
            source="config.patch.safe",
        )
        return cast(
            ModelRoutingMutation,
            {
                **self._policy.snapshot(live),
                "patched": list(candidate.patched),
                "restart_required": False,
            },
        )


class ProviderStatus:
    """Return the provider readiness projection for setup and diagnostics."""

    def __init__(self, port: ProviderStatusPort) -> None:
        self._port = port

    async def read(
        self,
        *,
        provider_id: str | None = None,
        probe_models: bool = False,
    ) -> ProviderStatusResult:
        provider = str(provider_id or "").strip() or None
        return cast(
            ProviderStatusResult,
            dict(
                await self._port.load_provider_status(
                    provider_id=provider,
                    probe_models=bool(probe_models),
                )
            ),
        )
