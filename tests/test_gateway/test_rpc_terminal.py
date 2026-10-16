from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway import rpc_terminal, websocket
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.guest_rpc_policy import GuestRpcPolicy, GuestRpcPolicyError
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.rpc_terminal import _handle_terminal_create, _handle_terminal_write


def _principal(*, host_execute: bool) -> Principal:
    capabilities = {"host.execute"} if host_execute else {"operator.write"}
    return Principal(
        role="operator",
        scopes=frozenset({"operator.write"}),
        is_owner=False,
        authenticated=True,
        capabilities=frozenset(capabilities),
    )


def test_terminal_methods_are_registered_with_operator_write() -> None:
    dispatcher = get_dispatcher()
    for method in ("terminal.create", "terminal.write", "terminal.resize", "terminal.close"):
        entry = dispatcher.get_entry(method)
        assert entry is not None
        assert entry.required_scope == "operator.write"


@pytest.mark.asyncio
async def test_terminal_create_requires_host_execute_before_workspace_access() -> None:
    context = RpcContext(
        conn_id="conn-1",
        principal=_principal(host_execute=False),
    )
    with pytest.raises(RpcHandlerError) as caught:
        await _handle_terminal_create({"sessionKey": "agent:main:webchat:terminal"}, context)
    assert caught.value.code == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_terminal_create_rejects_http_and_absolute_cwd() -> None:
    http_context = RpcContext(
        conn_id="http",
        principal=_principal(host_execute=True),
    )
    with pytest.raises(RpcHandlerError) as caught:
        await _handle_terminal_create({"sessionKey": "agent:main:webchat:terminal"}, http_context)
    assert caught.value.code == "INVALID_REQUEST"

    websocket_context = RpcContext(
        conn_id="conn-1",
        principal=_principal(host_execute=True),
    )
    with pytest.raises(RpcHandlerError) as caught:
        await _handle_terminal_create(
            {
                "sessionKey": "agent:main:webchat:terminal",
                "cwd": "C:\\outside",
            },
            websocket_context,
        )
    assert caught.value.code == "INVALID_REQUEST"


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [None, 42, False, {}, [], ""])
async def test_terminal_write_rejects_invalid_data_before_manager_access(
    monkeypatch: pytest.MonkeyPatch, data: object
) -> None:
    writer = AsyncMock(return_value={"accepted": True})
    monkeypatch.setattr("opensquilla.gateway.rpc_terminal._MANAGER.write", writer)
    context = RpcContext(conn_id="conn-1", principal=_principal(host_execute=True))

    with pytest.raises(RpcHandlerError) as caught:
        await _handle_terminal_write({"terminalId": "terminal-1", "data": data}, context)

    assert caught.value.code == "INVALID_REQUEST"
    writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_write_preserves_valid_input(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = AsyncMock(return_value={"accepted": True})
    monkeypatch.setattr("opensquilla.gateway.rpc_terminal._MANAGER.write", writer)
    context = RpcContext(conn_id="conn-1", principal=_principal(host_execute=True))
    data = " echo \u2713\r"

    assert await _handle_terminal_write(
        {"terminalId": "terminal-1", "data": data}, context
    ) == {"accepted": True}
    writer.assert_awaited_once_with(terminal_id="terminal-1", conn_id="conn-1", data=data)


def test_guest_policy_denies_all_terminal_control_methods() -> None:
    guest = Principal(
        role="operator",
        scopes=frozenset({"operator.read", "operator.write", "operator.admin"}),
        is_owner=False,
        authenticated=False,
        capabilities=frozenset({"guest.safe"}),
        auth_state="guest",
        guest_owner_id="a" * 64,
        guest_session_key="osqg_" + "A" * 43,
    )
    context = RpcContext(conn_id="guest", principal=guest)
    for method in ("terminal.create", "terminal.write", "terminal.resize", "terminal.close"):
        with pytest.raises(GuestRpcPolicyError):
            GuestRpcPolicy.authorize(method, {}, context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        {"kind": "default-agent", "agentId": "main"},
        {"kind": "project", "workspaceId": "project"},
    ],
)
async def test_standalone_terminal_requires_same_host_execute_capability(target) -> None:
    context = RpcContext(conn_id="conn-1", principal=_principal(host_execute=False))
    with pytest.raises(RpcHandlerError) as caught:
        await _handle_terminal_create({"target": target}, context)
    assert caught.value.code == "UNAUTHORIZED"



@pytest.mark.asyncio
async def test_terminal_create_routes_default_target_without_a_chat(tmp_path, monkeypatch):
    principal = _principal(host_execute=True)
    context = RpcContext(
        conn_id="standalone",
        principal=principal,
        config=GatewayConfig(workspace_dir=str(tmp_path)),
    )
    connection = SimpleNamespace(principal=principal)
    monkeypatch.setattr(
        websocket, "get_registry", lambda: SimpleNamespace(get=lambda _id: connection)
    )
    received = {}

    async def create(**kwargs):
        received.update(kwargs)
        workspace = await kwargs["workspace_service"].workspace()
        return {"terminalId": "term-standalone", "cwdPath": str(workspace.root)}

    fake_manager = SimpleNamespace(create=create, claim_connection_cleanup=lambda _id: False)
    monkeypatch.setattr(rpc_terminal, "_MANAGER", fake_manager)
    result = await _handle_terminal_create(
        {"target": {"kind": "default-agent", "agentId": "main"}},
        context,
    )
    assert received["session_key"] is None
    assert received["target"] == {"kind": "default-agent", "agentId": "main"}
    assert result["cwdPath"] == str(tmp_path.resolve())



@pytest.mark.asyncio
async def test_terminal_create_rejects_mixed_authority_before_spawn(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(rpc_terminal, "_MANAGER", SimpleNamespace(create=create))
    context = RpcContext(conn_id="conn-1", principal=_principal(host_execute=True))
    with pytest.raises(RpcHandlerError) as caught:
        await _handle_terminal_create(
            {
                "target": {"kind": "default-agent", "agentId": "main"},
                "sessionKey": "agent:main:webchat:other",
            },
            context,
        )
    assert caught.value.code == "INVALID_REQUEST"
    create.assert_not_awaited()
