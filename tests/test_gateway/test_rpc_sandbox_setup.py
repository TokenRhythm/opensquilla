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

    monkeypatch.setattr(rpc_sandbox, "ensure_sandbox_setup_auto", fake_ensure)

    payload = await rpc_sandbox._handle_sandbox_setup_ensure({}, _ctx())

    assert payload["state"] == "failed"
    assert payload["requiresAdmin"] is True


@pytest.mark.asyncio
async def test_run_context_set_requires_setup_for_sandbox_modes(monkeypatch) -> None:
    from opensquilla.gateway import rpc_sandbox
    from opensquilla.gateway.rpc import RpcHandlerError
    from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult

    async def fake_status(config):
        return SetupResult(
            state=SandboxSetupState.NOT_SETUP,
            platform="win32",
            message="Sandbox setup has not been completed.",
            requires_admin=True,
        )

    monkeypatch.setattr(rpc_sandbox, "current_sandbox_setup_status", fake_status)

    with pytest.raises(RpcHandlerError) as excinfo:
        await rpc_sandbox._handle_sandbox_run_context_set(
            {"sessionKey": "agent:main:webchat:default", "runMode": "trusted"},
            _ctx(),
        )

    assert excinfo.value.code == "SANDBOX_SETUP_REQUIRED"
