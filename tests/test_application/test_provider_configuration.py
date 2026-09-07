"""Tests for provider configuration application Modules."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.application.provider_configuration import (
    ModelCatalog,
    ModelRouting,
    PreparedModelRouting,
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
    events: list[str] = []

    class ConfigPort:
        def __init__(self) -> None:
            self.config = SimpleNamespace(mode="fixed")

        def active_config(self) -> Any:
            return self.config

        def persist_candidate(self, candidate: Any, **_kwargs: Any) -> str:
            events.append("persist")
            return "/tmp/config.toml"

        def install_candidate(self, candidate: Any) -> Any:
            events.append("install")
            self.config = candidate
            return candidate

    class PolicyPort:
        def snapshot(self, config: Any) -> Mapping[str, Any]:
            return {"mode": config.mode}

        def prepare(self, _config: Any, mode: str) -> PreparedModelRouting:
            events.append("prepare-candidate")
            return PreparedModelRouting(
                SimpleNamespace(mode=mode),
                ("squilla_router.enabled",),
            )

    class RuntimePort:
        def prepare_reconciliation(self, config: Any) -> Any:
            events.append("prepare-runtime")
            return config.mode

        async def reconcile(self, config: Any, prepared: Any) -> None:
            events.append(f"reconcile:{prepared}:{config.mode}")

        async def publish_changed(
            self,
            previous: Mapping[str, Any],
            config: Any,
            *,
            source: str,
        ) -> None:
            events.append(f"publish:{source}:{previous['mode']}:{config.mode}")

    routing = ModelRouting(ConfigPort(), PolicyPort(), RuntimePort())

    assert await routing.read() == {"mode": "fixed"}
    result = await routing.set_mode("  auto  ")
    assert result == {
        "mode": "auto",
        "patched": ["squilla_router.enabled"],
        "restart_required": False,
    }
    assert events == [
        "prepare-candidate",
        "prepare-runtime",
        "persist",
        "install",
        "reconcile:auto:auto",
        "publish:config.patch.safe:fixed:auto",
    ]
    with pytest.raises(ValueError, match="routing mode is required"):
        await routing.set_mode("  ")


@pytest.mark.asyncio
async def test_routing_persist_failure_stops_before_live_reconciliation() -> None:
    events: list[str] = []
    current = SimpleNamespace(mode="direct")

    class ConfigPort:
        def active_config(self) -> Any:
            return current

        def persist_candidate(self, _candidate: Any, **_kwargs: Any) -> str:
            events.append("persist")
            raise OSError("disk full")

        def install_candidate(self, candidate: Any) -> Any:
            events.append("install")
            return candidate

    class PolicyPort:
        def snapshot(self, config: Any) -> Mapping[str, Any]:
            return {"mode": config.mode}

        def prepare(self, _config: Any, mode: str) -> PreparedModelRouting:
            return PreparedModelRouting(SimpleNamespace(mode=mode), ("routing",))

    class RuntimePort:
        def prepare_reconciliation(self, _config: Any) -> Any:
            events.append("prepare-runtime")
            return None

        async def reconcile(self, _config: Any, _prepared: Any) -> None:
            events.append("reconcile")

        async def publish_changed(
            self,
            _previous: Mapping[str, Any],
            _config: Any,
            *,
            source: str,
        ) -> None:
            events.append(f"publish:{source}")

    routing = ModelRouting(ConfigPort(), PolicyPort(), RuntimePort())
    with pytest.raises(OSError, match="disk full"):
        await routing.set_mode("router")

    assert current.mode == "direct"
    assert events == ["prepare-runtime", "persist"]


@pytest.mark.asyncio
async def test_provider_status_normalizes_optional_provider_and_probe_intent() -> None:
    port = ProviderPort()
    status = ProviderStatus(port)

    await status.read(provider_id="  openai  ", probe_models=True)
    await status.read(provider_id="")

    assert port.status_queries == [("openai", True), (None, False)]
