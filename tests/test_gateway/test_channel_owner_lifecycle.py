from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway import boot
from opensquilla.gateway.config import GatewayConfig


class _Channels:
    _channels = {"fixture": object()}

    def __init__(self, result: dict[str, bool] | Exception) -> None:
        self.result = result

    async def start_all(self) -> dict[str, bool]:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def stop_all(self, *, timeout: float | None = None) -> None:
        pass


async def _start_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manager: Any,
) -> boot.GatewayServer:
    class TurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def set_session_lock_provider(self, provider: Any) -> None:
            pass

    async def build_services(**kwargs: Any) -> boot.ServiceContainer:
        return boot.ServiceContainer(
            config=kwargs["config"],
            provider_selector=object(),
            tool_registry=object(),
            session_manager=object(),
            skill_loader=object(),
            usage_tracker=object(),
            config_revision=7,
        )

    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", TurnRunner)
    monkeypatch.setattr(boot, "build_services", build_services)
    monkeypatch.setattr(boot, "get_session_storage", lambda manager: None)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "_ensure_sandbox_setup_on_boot", AsyncMock())
    monkeypatch.setattr(
        "opensquilla.observability.update_check.start_background_update_check",
        lambda **kwargs: None,
    )
    monkeypatch.setattr("opensquilla.gateway.pidlock.GatewayPidLock.acquire", lambda self: None)
    monkeypatch.setattr("opensquilla.gateway.pidlock.GatewayPidLock.release", lambda self: None)
    return await boot.start_gateway_server(
        config=GatewayConfig(
            state_dir=str(tmp_path / "state"),
            workspace_dir=str(tmp_path / "workspace"),
            control_ui={"enabled": False},
            channels={"channels": []},
        ),
        channel_manager=manager,
        run=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (None, "disabled"),
        ({"fixture": True}, "ready"),
        ({"fixture": False}, "degraded"),
        (OSError("synthetic startup failure"), "degraded"),
    ],
)
async def test_channel_owner_shares_terminal_status_and_shutdown_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: Any, expected: str,
) -> None:
    server = await _start_server(
        tmp_path, monkeypatch, None if result is None else _Channels(result),
    )
    services = server._services
    assert services is not None
    owner = services.optional_owners["channels"]
    descriptor = services.optional_services["channels"]
    try:
        await services.optional_start_task
        assert owner.descriptor is descriptor
        assert server.app.state.optional_services["channels"] is descriptor
        assert descriptor["status"] == descriptor["state"] == owner.state == expected
        assert descriptor["config_revision"] == 7
        assert descriptor["generation"] == services.optional_generation == 1
        assert descriptor["owner_generation"] == 1
        assert server.app.state.legacy_ready is True
    finally:
        await server.close()
    assert services.optional_services["channels"] is descriptor
    assert descriptor["status"] == descriptor["state"] == owner.state == "stopped"


@pytest.mark.asyncio
async def test_channel_owner_retry_preserves_service_generation_and_clears_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _Channels(OSError("first attempt"))
    server = await _start_server(tmp_path, monkeypatch, manager)
    services = server._services
    assert services is not None
    try:
        await services.optional_start_task
        owner = services.optional_owners["channels"]
        descriptor = services.optional_services["channels"]
        assert descriptor["error"] == "OSError"
        manager.result = {"fixture": True}
        assert await owner.start_once() == "ready"
        assert services.optional_services["channels"] is descriptor
        assert descriptor["status"] == descriptor["state"] == "ready"
        assert descriptor["generation"] == 1
        assert descriptor["owner_generation"] == 2
        assert descriptor["channels"] == {"fixture": True}
        assert "error" not in descriptor

        manager.result = RuntimeError("third attempt")
        assert await owner.start_once() == "degraded"
        assert descriptor["owner_generation"] == 3
        assert descriptor["error"] == "RuntimeError"
        assert "channels" not in descriptor
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_channel_owner_late_start_cannot_overwrite_closed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class ResistantChannels(_Channels):
        async def start_all(self) -> dict[str, bool]:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return {"fixture": True}

    server = await _start_server(tmp_path, monkeypatch, ResistantChannels({}))
    services = server._services
    assert services is not None
    startup = services.optional_start_task
    try:
        await asyncio.wait_for(started.wait(), 1)
        owner = services.optional_owners["channels"]
        descriptor = services.optional_services["channels"]
        await owner.close()
        assert descriptor["status"] == descriptor["state"] == "stopped"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await startup
        assert services.optional_services["channels"] is descriptor
        assert descriptor["status"] == descriptor["state"] == "stopped"
    finally:
        release.set()
        await server.close()
