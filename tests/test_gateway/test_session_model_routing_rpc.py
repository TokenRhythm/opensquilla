"""Gateway contracts for durable per-session model routing."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from opensquilla.gateway.adapters.pending_input_queue import GatewayPendingInputQueueAdapter
from opensquilla.gateway.admission_input import decode_admit_turn
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.model_routing import model_routing_snapshot
from opensquilla.gateway.pending_input_primitives import pending_input_payload
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.rpc_sessions import (
    _handle_sessions_delete,
    _handle_sessions_routing_get,
    _handle_sessions_routing_set,
)
from opensquilla.gateway.scopes import METHOD_SCOPES, READ_SCOPE, WRITE_SCOPE
from opensquilla.gateway.session_model_routing import capture_accepted_model_routing_config
from opensquilla.router_control import RouterControlHoldStore
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage
from opensquilla.tool_boundary import ToolCall
from opensquilla.tools import get_default_registry
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.types import ToolContext


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


@pytest.mark.asyncio
@pytest.mark.parametrize("old_mode,new_mode", [
    (old, new) for old in ("direct", "router", "ensemble")
    for new in ("direct", "router", "ensemble") if old != new
])
async def test_session_mode_changes_clear_router_hold_without_replaying_side_effect(
    old_mode, new_mode,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, model_routing_mode_provider=lambda: old_mode)
        key = "agent:main:webchat:routing-rpc-hold-lifecycle"
        await manager.create(key)
        config = GatewayConfig()
        hold_store = RouterControlHoldStore()
        target = hold_store.build_targets(config.squilla_router)[1]
        runner = SimpleNamespace(router_control_hold_store=hold_store)
        ctx = RpcContext(
            conn_id="routing-rpc-hold-lifecycle",
            config=config,
            session_manager=manager,
            turn_runner=runner,
        )
        accepted = await capture_accepted_model_routing_config(
            config, manager, session_key=key, run_kind="web_turn",
        )

        hold_store.set_hold(key, target, evidence="synthetic stale hold")
        changed = await _handle_sessions_routing_set(
            {"sessionKey": key, "mode": new_mode, "expectedRevision": 0},
            ctx,
        )
        assert changed["routing"]["revision"] == 1
        assert "changed" not in changed
        assert "changed" not in changed["routing"]
        assert hold_store.get_valid(key) is None
        assert model_routing_snapshot(accepted)["mode"] == old_mode
        next_accepted = await capture_accepted_model_routing_config(
            config, manager, session_key=key, run_kind="web_turn",
        )
        assert model_routing_snapshot(next_accepted)["mode"] == new_mode

        # A lost-ack retry did not change strategy and must not erase a hold
        # created after the successful write.
        hold_store.set_hold(key, target, evidence="synthetic current hold")
        replay = await _handle_sessions_routing_set(
            {"sessionKey": key, "mode": new_mode, "expectedRevision": 0},
            ctx,
        )
        assert replay["routing"]["revision"] == 1
        assert hold_store.get_valid(key) is not None

        changed_again = await _handle_sessions_routing_set(
            {"sessionKey": key, "mode": old_mode, "expectedRevision": 1},
            ctx,
        )
        assert changed_again["routing"]["revision"] == 2
        assert hold_store.get_valid(key) is None
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["set_hold", "clear_hold"])
async def test_old_turn_cannot_mutate_hold_after_mode_switch(action: str) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, model_routing_mode_provider=lambda: "router")
        key = "agent:main:webchat:routing-stale-turn"
        await manager.create(key)
        config = GatewayConfig()
        config.squilla_router.enabled = True
        store = RouterControlHoldStore()
        tool_ctx = ToolContext(
            session_key=key,
            router_control_config=config.squilla_router,
            router_control_hold_store=store,
        )
        # The tool call belongs to a turn admitted before either mode switch.
        tool_ctx.router_control_routing_revision = 0
        handler = build_tool_handler(get_default_registry(), tool_ctx)
        rpc_ctx = RpcContext(
            conn_id="routing-stale-turn",
            config=config,
            session_manager=manager,
            turn_runner=SimpleNamespace(router_control_hold_store=store),
        )
        await _handle_sessions_routing_set(
            {"sessionKey": key, "mode": "direct", "expectedRevision": 0}, rpc_ctx
        )
        await _handle_sessions_routing_set(
            {"sessionKey": key, "mode": "router", "expectedRevision": 1}, rpc_ctx
        )
        target = store.build_targets(config.squilla_router)[0]
        current_hold = store.set_hold(key, target, evidence="synthetic current hold")

        result = await handler(
            ToolCall(
                tool_use_id="stale-call",
                tool_name="router_control",
                arguments={
                    "action": action,
                    "target_id": "tier:c3",
                    "evidence": "synthetic old request",
                },
            )
        )

        assert json.loads(result.content)["accepted"] is False
        assert result.terminates_turn is False
        assert store.get_valid(key) is current_hold
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_deleted_session_drops_hold_and_revision_barrier() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, model_routing_mode_provider=lambda: "router")
        key = "agent:main:webchat:routing-deleted"
        await manager.create(key)
        config = GatewayConfig()
        store = RouterControlHoldStore()
        store.advance_routing_revision(key, 4)
        store.set_hold(
            key, store.build_targets(config.squilla_router)[0],
            evidence="synthetic deleted hold",
        )
        ctx = RpcContext(
            conn_id="routing-deleted",
            config=config,
            session_manager=manager,
            turn_runner=SimpleNamespace(router_control_hold_store=store),
        )

        result = await _handle_sessions_delete({"key": key}, ctx)

        assert result["deleted"] == [key]
        assert store.get_valid(key) is None
        await manager.create(key)
        assert store.is_current_revision(key, 0) is True
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_session_routing_set_preserves_legacy_null_cas_boundary() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        key = "agent:main:webchat:routing-rpc-legacy-null"
        await storage.upsert_session(
            SessionNode(
                session_key=key,
                session_id="routing-rpc-legacy-null",
                agent_id="main",
            )
        )
        manager = SessionManager(storage, model_routing_mode_provider=lambda: "direct")
        changed = await _handle_sessions_routing_set(
            {"sessionKey": key, "mode": "router", "expectedRevision": 0},
            RpcContext(
                conn_id="routing-rpc-legacy-null",
                config=GatewayConfig(),
                session_manager=manager,
            ),
        )

        assert changed["routing"]["mode"] == "router"
        assert changed["routing"]["revision"] == 1
        persisted = await storage.get_session(key)
        assert persisted is not None
        assert persisted.model_routing_mode == "router"
        assert persisted.model_routing_revision == 1
    finally:
        await storage.close()


def test_session_routing_rpc_scopes_are_explicit() -> None:
    assert METHOD_SCOPES["sessions.routing.get"] == READ_SCOPE
    assert METHOD_SCOPES["sessions.routing.set"] == WRITE_SCOPE


def test_pending_input_payload_preserves_initial_routing_mode() -> None:
    turn = decode_admit_turn(
        {
            "key": "agent:main:webchat:routing-pending",
            "message": "queued first turn",
            "clientRequestId": "routing-pending-request",
            "clientMessageId": "routing-pending-message",
            "intent": "new_chat",
            "initialRoutingMode": "ensemble",
        },
    )
    payload = pending_input_payload(turn, False)

    assert payload["initialRoutingMode"] == "ensemble"


@pytest.mark.asyncio
async def test_pending_input_rejects_new_session_routing_before_staging() -> None:
    adapter = GatewayPendingInputQueueAdapter(object())

    with pytest.raises(RpcHandlerError) as caught:
        await adapter.enqueue(
            {
                "key": "agent:main:webchat:routing-pending",
                "pendingInputId": "routing-pending-input",
                "clientRequestId": "routing-pending-request",
                "clientMessageId": "routing-pending-message",
                "message": "queued first turn",
                "intent": "new_chat",
                "initialRoutingMode": "ensemble",
            }
        )

    assert caught.value.code == "PENDING_INITIAL_ROUTING_UNSUPPORTED"
    assert caught.value.retryable is False
