"""Gateway contracts for durable per-session model routing."""

from __future__ import annotations

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.rpc_sessions import (
    _handle_pending_inputs_enqueue,
    _handle_sessions_routing_get,
    _handle_sessions_routing_set,
    _pending_input_send_payload,
)
from opensquilla.gateway.scopes import METHOD_SCOPES, READ_SCOPE, WRITE_SCOPE
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage


@pytest.mark.asyncio
async def test_session_routing_get_set_cas_and_lost_ack_retry() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, model_routing_mode_provider=lambda: "direct")
        key = "agent:main:webchat:routing-rpc"
        await manager.create(key)
        ctx = RpcContext(
            conn_id="routing-rpc",
            config=GatewayConfig(),
            session_manager=manager,
        )

        loaded = await _handle_sessions_routing_get({"sessionKey": key}, ctx)
        assert loaded["routing"]["mode"] == "direct"
        assert loaded["routing"]["revision"] == 0

        changed = await _handle_sessions_routing_set(
            {"sessionKey": key, "mode": "router", "expectedRevision": 0},
            ctx,
        )
        assert changed["routing"]["mode"] == "router"
        assert changed["routing"]["revision"] == 1

        replay = await _handle_sessions_routing_set(
            {"sessionKey": key, "mode": "router", "expectedRevision": 0},
            ctx,
        )
        assert replay["routing"]["revision"] == 1

        with pytest.raises(RpcHandlerError, match="model routing changed"):
            await _handle_sessions_routing_set(
                {"sessionKey": key, "mode": "direct", "expectedRevision": 0},
                ctx,
            )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_session_routing_set_requires_expected_revision() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, model_routing_mode_provider=lambda: "direct")
        key = "agent:main:webchat:routing-rpc-required-revision"
        await manager.create(key)
        ctx = RpcContext(
            conn_id="routing-rpc-required-revision",
            config=GatewayConfig(),
            session_manager=manager,
        )

        with pytest.raises(ValueError, match="expectedRevision"):
            await _handle_sessions_routing_set(
                {"sessionKey": key, "mode": "router"},
                ctx,
            )

        loaded = await _handle_sessions_routing_get({"sessionKey": key}, ctx)
        assert loaded["routing"]["mode"] == "direct"
        assert loaded["routing"]["revision"] == 0
    finally:
        await storage.close()


def test_session_routing_rpc_scopes_are_explicit() -> None:
    assert METHOD_SCOPES["sessions.routing.get"] == READ_SCOPE
    assert METHOD_SCOPES["sessions.routing.set"] == WRITE_SCOPE


def test_pending_input_payload_preserves_initial_routing_mode() -> None:
    payload = _pending_input_send_payload(
        {
            "message": "queued first turn",
            "clientRequestId": "routing-pending-request",
            "clientMessageId": "routing-pending-message",
            "intent": "new_chat",
            "initialRoutingMode": "ensemble",
        },
        key="agent:main:webchat:routing-pending",
    )

    assert payload["initialRoutingMode"] == "ensemble"


@pytest.mark.asyncio
async def test_pending_input_rejects_new_session_routing_before_staging() -> None:
    ctx = RpcContext(conn_id="routing-pending", config=GatewayConfig())

    with pytest.raises(RpcHandlerError) as caught:
        await _handle_pending_inputs_enqueue(
            {
                "key": "agent:main:webchat:routing-pending",
                "pendingInputId": "routing-pending-input",
                "clientRequestId": "routing-pending-request",
                "clientMessageId": "routing-pending-message",
                "message": "queued first turn",
                "intent": "new_chat",
                "initialRoutingMode": "ensemble",
            },
            ctx,
        )

    assert caught.value.code == "PENDING_INITIAL_ROUTING_UNSUPPORTED"
    assert caught.value.retryable is False
