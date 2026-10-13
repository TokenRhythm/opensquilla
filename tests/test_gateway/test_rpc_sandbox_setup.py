from __future__ import annotations

from types import SimpleNamespace

import pytest


class _Principal:
    is_owner = True


class _SessionManager:
    def __init__(self) -> None:
        self.node = SimpleNamespace(
            session_key="agent:main:webchat:default",
            agent_id="main",
            origin=None,
        )
        self.sessions = {self.node.session_key: self.node}

    async def get_session(self, session_key: str):
        return self.sessions.get(session_key)

    async def update(self, session_key: str, **fields):
        node = self.sessions[session_key]
        for key, value in fields.items():
            setattr(node, key, value)
        return node


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(workspace_dir="/tmp/ws", agents=[]),
        principal=_Principal(),
        session_manager=_SessionManager(),
    )


@pytest.mark.asyncio
async def test_sandbox_setup_status_returns_platform_payload(monkeypatch) -> None:
    from opensquilla.gateway import rpc_sandbox
    from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult

    async def fake_status(config):
        return SetupResult(
            state=SandboxSetupState.NOT_SETUP,
            platform="win32",
            message="Sandbox setup has not been completed.",
            requires_admin=True,
        )

    monkeypatch.setattr(rpc_sandbox, "current_sandbox_setup_runtime_status", fake_status)

    payload = await rpc_sandbox._handle_sandbox_setup_status({}, _ctx())

    assert payload["state"] == "not_setup"
    assert payload["requiresAdmin"] is True


@pytest.mark.asyncio
async def test_setup_does_not_hold_the_same_connection_ordinary_queue(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from opensquilla.gateway import rpc_sandbox
    from opensquilla.gateway.websocket import WsConnection
    from opensquilla.sandbox import setup_runtime
    from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult

    setup_runtime.reset_sandbox_setup_runtime_state()
    release = asyncio.Event()
    completed = []
    setup_calls = 0

    async def blocked_setup(_config):
        nonlocal setup_calls
        setup_calls += 1
        await release.wait()
        return SetupResult(SandboxSetupState.READY, "win32", "ready")

    monkeypatch.setattr(setup_runtime, "ensure_sandbox_setup", blocked_setup)
    monkeypatch.setattr("opensquilla.sandbox.integration.initialize_runtime_backend", AsyncMock())
    connection = SimpleNamespace(
        _ordinary_queue=asyncio.Queue(),
        _ordinary_stopped=False,
        _ordinary_worker=None,
        release_transport_bytes=lambda _size: None,
    )
    ctx = _ctx()

    async def ensure():
        result = await rpc_sandbox._handle_sandbox_setup_ensure({}, ctx)
        assert result["state"] == "setting_up"
        completed.append("ensure")

    async def status():
        result = await rpc_sandbox._handle_sandbox_setup_status({}, ctx)
        assert result["state"] == "setting_up"
        completed.append("status")

    async def ordinary_request(name):
        completed.append(name)

    try:
        for request in (ensure(), status(), ordinary_request("Full"), ordinary_request("chat")):
            connection._ordinary_queue.put_nowait((request, 0, None, None))
        # Exercise the production serialized queue, not concurrent direct calls.
        worker = asyncio.create_task(WsConnection._run_ordinary_requests(connection))
        connection._ordinary_worker = worker
        await asyncio.wait_for(worker, 1)
        await asyncio.sleep(0)
        assert completed == ["ensure", "status", "Full", "chat"]
        assert setup_calls == 1
        assert not release.is_set()
        await rpc_sandbox._handle_sandbox_setup_ensure({}, ctx)
        assert setup_calls == 1
    finally:
        release.set()
        if setup_runtime._SETUP_TASK:
            await setup_runtime._SETUP_TASK
        setup_runtime.reset_sandbox_setup_runtime_state()


@pytest.mark.asyncio
async def test_identity_repair_requires_explicit_boolean_parameter(monkeypatch):
    from unittest.mock import AsyncMock

    from opensquilla.gateway import rpc_sandbox
    from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult

    runner = AsyncMock(return_value=SetupResult(SandboxSetupState.SETTING_UP, "win32", "running"))
    monkeypatch.setattr(rpc_sandbox, "request_sandbox_setup", runner)
    ctx = _ctx()
    await rpc_sandbox._handle_sandbox_setup_ensure({}, ctx)
    runner.assert_awaited_with(ctx.config)
    await rpc_sandbox._handle_sandbox_setup_ensure({"repairIdentity": True}, ctx)
    runner.assert_awaited_with(ctx.config, repair_identity=True)
    with pytest.raises(ValueError, match="boolean"):
        await rpc_sandbox._handle_sandbox_setup_ensure({"repairIdentity": "true"}, ctx)


@pytest.mark.asyncio
async def test_sandbox_setup_status_returns_setting_up_payload(monkeypatch) -> None:
    from opensquilla.gateway import rpc_sandbox
    from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult

    async def fake_status(config):
        return SetupResult(
            state=SandboxSetupState.SETTING_UP,
            platform="auto",
            message="Sandbox setup is running.",
            requires_admin=False,
        )

    monkeypatch.setattr(rpc_sandbox, "current_sandbox_setup_runtime_status", fake_status)

    payload = await rpc_sandbox._handle_sandbox_setup_status({}, _ctx())

    assert payload["state"] == "setting_up"
    assert payload["platform"] == "auto"
    assert payload["requiresAdmin"] is False


@pytest.mark.asyncio
async def test_sandbox_capability_status_forwards_explicit_refresh(monkeypatch) -> None:
    from opensquilla.gateway import rpc_sandbox
    from opensquilla.sandbox.capability_service import (
        REQUIRED_SAFE_CAPABILITIES,
        WINDOWS_REQUIRED_SAFE_CAPABILITIES,
        CapabilityReport,
    )

    refresh_values: list[bool] = []

    async def fake_status(config, *, force_refresh=False):
        refresh_values.append(force_refresh)
        return CapabilityReport.available_for(
            backend="windows_native",
            platform="win32",
            capabilities=REQUIRED_SAFE_CAPABILITIES | WINDOWS_REQUIRED_SAFE_CAPABILITIES,
        )

    monkeypatch.setattr(rpc_sandbox, "current_sandbox_capability_report", fake_status)

    payload = await rpc_sandbox._handle_sandbox_capability_status(
        {"refresh": True},
        _ctx(),
    )

    assert payload["available"] is True
    assert refresh_values == [True]


@pytest.mark.asyncio
async def test_sandbox_setup_ensure_returns_platform_payload(monkeypatch) -> None:
    from opensquilla.gateway import rpc_sandbox
    from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult

    async def fake_ensure(config):
        return SetupResult(
            state=SandboxSetupState.FAILED,
            platform="win32",
            message="Windows sandbox service setup is not available.",
            requires_admin=True,
        )

    monkeypatch.setattr(rpc_sandbox, "request_sandbox_setup", fake_ensure)

    payload = await rpc_sandbox._handle_sandbox_setup_ensure({}, _ctx())

    assert payload["state"] == "failed"
    assert payload["requiresAdmin"] is True


@pytest.mark.asyncio
async def test_run_context_set_requires_setup_for_sandbox_modes(monkeypatch) -> None:
    from opensquilla.gateway import rpc_sandbox
    from opensquilla.gateway.rpc import RpcHandlerError
    from opensquilla.sandbox.capability_service import CapabilityReport

    async def fake_status(config):
        return CapabilityReport(
            available=False,
            backend="windows_native",
            platform="win32",
            code="not_setup",
            reason="Sandbox setup has not been completed.",
            setup_supported=True,
            restart_required=False,
            probe_version=1,
            capabilities=frozenset(),
        )

    monkeypatch.setattr(rpc_sandbox, "current_sandbox_capability_report", fake_status)

    with pytest.raises(RpcHandlerError) as excinfo:
        await rpc_sandbox._handle_sandbox_run_context_set(
            {"sessionKey": "agent:main:webchat:default", "runMode": "trusted"},
            _ctx(),
        )

    assert excinfo.value.code == "SANDBOX_CAPABILITY_UNAVAILABLE"
