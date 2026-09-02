"""Application Modules for provider, model-catalog and routing use cases."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class ModelCatalogPort(Protocol):
    async def load_model_catalog(self) -> Mapping[str, Any]: ...


class ModelRoutingPort(Protocol):
    async def read_model_routing(self) -> Mapping[str, Any]: ...

    async def write_model_routing(self, mode: str) -> Mapping[str, Any]: ...


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
    """Read and change the operator's routing intent."""

    def __init__(self, port: ModelRoutingPort) -> None:
        self._port = port

    async def read(self) -> dict[str, Any]:
        return dict(await self._port.read_model_routing())

    async def set_mode(self, mode: str) -> dict[str, Any]:
        normalized = str(mode or "").strip()
        if not normalized:
            raise ValueError("routing mode is required")
        return dict(await self._port.write_model_routing(normalized))


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
