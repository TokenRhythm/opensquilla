"""Tests for provider configuration application Modules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from opensquilla.application.provider_configuration import (
    ModelCatalog,
    ModelRouting,
    ProviderStatus,
)


class ProviderPort:
    def __init__(self) -> None:
        self.routing_modes: list[str] = []
        self.status_queries: list[tuple[str | None, bool]] = []

    async def load_model_catalog(self) -> Mapping[str, Any]:
        return {
            "models": [
                {"id": "a", "provider": "one", "capabilities": ["chat", "tools"]},
                {"id": "b", "provider": "two", "capabilities": ["chat"]},
            ],
            "errors": [{"provider": "three", "kind": "unavailable"}],
        }

    async def read_model_routing(self) -> Mapping[str, Any]:
        return {"mode": "fixed"}

    async def write_model_routing(self, mode: str) -> Mapping[str, Any]:
        self.routing_modes.append(mode)
        return {"mode": mode, "restart_required": False}

    async def load_provider_status(
        self, *, provider_id: str | None, probe_models: bool
    ) -> Mapping[str, Any]:
        self.status_queries.append((provider_id, probe_models))
        return {"activeProvider": provider_id or "one", "providers": []}


@pytest.mark.asyncio
async def test_model_catalog_filters_models_but_preserves_errors() -> None:
    port = ProviderPort()

    result = await ModelCatalog(port).query(provider_id="one", capabilities=["tools"])

    assert [row["id"] for row in result["models"]] == ["a"]
    assert result["errors"] == [{"provider": "three", "kind": "unavailable"}]


@pytest.mark.asyncio
async def test_routing_accepts_only_a_nonempty_domain_mode() -> None:
    port = ProviderPort()
    routing = ModelRouting(port)

    assert await routing.read() == {"mode": "fixed"}
    assert (await routing.set_mode("  auto  "))["mode"] == "auto"
    with pytest.raises(ValueError, match="routing mode is required"):
        await routing.set_mode("  ")
    assert port.routing_modes == ["auto"]


@pytest.mark.asyncio
async def test_provider_status_normalizes_optional_provider_and_probe_intent() -> None:
    port = ProviderPort()
    status = ProviderStatus(port)

    await status.read(provider_id="  openai  ", probe_models=True)
    await status.read(provider_id="")

    assert port.status_queries == [("openai", True), (None, False)]
