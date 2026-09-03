"""Application Modules for provider, model-catalog and routing use cases."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from opensquilla.application.setup_mutations import SetupConfigPort


class ModelCatalogPort(Protocol):
    async def load_model_catalog(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PreparedModelRouting:
    config: Any
    patched: tuple[str, ...]


class ModelRoutingPolicyPort(Protocol):
    def snapshot(self, config: Any) -> Mapping[str, Any]: ...

    def prepare(self, config: Any, mode: str) -> PreparedModelRouting: ...


class ModelRoutingRuntimePort(Protocol):
    def prepare_reconciliation(self, config: Any) -> Any: ...

    async def reconcile(self, config: Any, prepared: Any) -> None: ...

    async def publish_changed(
        self,
        previous: Mapping[str, Any],
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
    ) -> Mapping[str, Any]: ...


class ModelCatalog:
    """Query and filter the public model catalog without wire knowledge."""

    def __init__(self, port: ModelCatalogPort) -> None:
        self._port = port

    async def query(
        self,
        *,
        provider_id: str | None = None,
        capabilities: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        result = await self._port.load_model_catalog()
        models = [dict(row) for row in result.get("models", ()) if isinstance(row, Mapping)]
        errors = [dict(row) for row in result.get("errors", ()) if isinstance(row, Mapping)]
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
        return {"models": models, "errors": errors}


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

    async def read(self) -> dict[str, Any]:
        return dict(self._policy.snapshot(self._config.active_config()))

    async def set_mode(self, mode: str) -> dict[str, Any]:
        normalized = str(mode or "").strip()
        if not normalized:
            raise ValueError("routing mode is required")
        current = self._config.active_config()
        previous = dict(self._policy.snapshot(current))
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
        return {
            **self._policy.snapshot(live),
            "patched": list(candidate.patched),
            "restart_required": False,
        }


class ProviderStatus:
    """Return the provider readiness projection for setup and diagnostics."""

    def __init__(self, port: ProviderStatusPort) -> None:
        self._port = port

    async def read(
        self,
        *,
        provider_id: str | None = None,
        probe_models: bool = False,
    ) -> dict[str, Any]:
        provider = str(provider_id or "").strip() or None
        return dict(
            await self._port.load_provider_status(
                provider_id=provider,
                probe_models=bool(probe_models),
            )
        )
