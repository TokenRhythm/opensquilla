"""Existing conversations switch deployment atomically only at an idle boundary."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.rpc_sessions import (
    _handle_sessions_routing_set,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus
from opensquilla.session.storage import SessionStorage


@pytest.fixture
async def setup():
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, model_routing_mode_provider=lambda: "direct")
    key = "agent:main:webchat:model-change"
    session = await manager.create(key, model="first", provider_override="openai")
    await manager.append_message(key, "user", "Keep the complete conversation")
    await manager.append_message(key, "assistant", "First reply")
    ctx = RpcContext(conn_id="model-change", config=GatewayConfig(), session_manager=manager)
    yield storage, manager, session, ctx
    await storage.close()


def params(session, selection, *, revision=0, mode="direct"):
    return {"sessionKey": session.session_key, "mode": mode,
            "expectedRevision": revision, "modelSelection": selection}


@pytest.mark.asyncio
async def test_switch_pair_preserves_history_and_session_and_retries_atomically(setup):
    storage, manager, before, ctx = setup
    history = await storage.get_canonical_transcript(before.session_id)
    global_config = ctx.config.model_dump()
    selection = {"model": "deepseek-chat", "provider": "deepseek"}
    result = await _handle_sessions_routing_set(params(before, selection), ctx)
    assert result["routing"]["modelSelection"] == selection
    assert result["routing"]["revision"] == 1
    after = await manager.get_session(before.session_key)
    assert (after.session_id, after.epoch) == (before.session_id, before.epoch)
    assert (after.model, after.provider_override) == ("deepseek-chat", "deepseek")
    assert await storage.get_canonical_transcript(before.session_id) == history
    assert ctx.config.model_dump() == global_config
    retry = await _handle_sessions_routing_set(params(before, selection), ctx)
    assert retry["revision"] == 1
    with pytest.raises(RpcHandlerError) as caught:
        await _handle_sessions_routing_set(
            params(before, {"model": "other", "provider": "openai"}), ctx,
        )
    assert caught.value.code == "SESSION_ROUTING_CHANGED"
    assert caught.value.details["routing"]["modelSelection"] == selection


@pytest.mark.asyncio
async def test_same_model_different_provider_and_reset_are_distinct_choices(setup):
    _, manager, session, ctx = setup
    result = await _handle_sessions_routing_set(
        params(session, {"model": "first", "provider": "openrouter"}), ctx,
    )
    assert result["revision"] == 1
    assert (await manager.get_session(session.session_key)).provider_override == "openrouter"
    result = await _handle_sessions_routing_set(params(session, None, revision=1), ctx)
    assert result["modelSelection"] is None
    assert result["revision"] == 2
    after = await manager.get_session(session.session_key)
    assert after.model is None and after.provider_override is None


@pytest.mark.asyncio
async def test_routing_only_preserves_saved_single_model_choice(setup):
    _, manager, session, ctx = setup
    for revision, mode in enumerate(("router", "ensemble", "direct")):
        result = await _handle_sessions_routing_set({
            "sessionKey": session.session_key, "mode": mode, "expectedRevision": revision,
        }, ctx)
        assert result["modelSelection"] == {"model": "first", "provider": "openai"}
    assert (await manager.get_session(session.session_key)).model == "first"


@pytest.mark.asyncio
@pytest.mark.parametrize("selection,mode", [
    ({"model": "x", "provider": "openai"}, "router"),
    ({"model": "x", "provider": "unknown-provider"}, "direct"),
    ({"model": "x"}, "direct"),
    ({"model": " ", "provider": "openai"}, "direct"),
    ({"model": "x", "provider": None}, "direct"),
    ({"model": "x", "provider": "openai", "authProfile": "secret"}, "direct"),
])
async def test_invalid_selection_has_no_partial_mode_or_binding_mutation(setup, selection, mode):
    _, manager, session, ctx = setup
    with pytest.raises((ValueError, RpcHandlerError)):
        await _handle_sessions_routing_set(params(session, selection, mode=mode), ctx)
    stored = await manager.get_session(session.session_key)
    persisted = (
        stored.model, stored.provider_override,
        stored.model_routing_mode, stored.model_routing_revision,
    )
    assert persisted == (
        "first", "openai", "direct", 0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [AgentTaskStatus.RUNNING, AgentTaskStatus.QUEUED])
async def test_durable_busy_after_over_one_hundred_completed_tasks_fails_closed(setup, status):
    storage, manager, session, ctx = setup
    for index in range(101):
        await storage.create_agent_task(AgentTaskRecord(
            task_id=f"complete-{index}", session_key=session.session_key,
            status=AgentTaskStatus.SUCCEEDED, created_at=index,
        ))
    await storage.create_agent_task(AgentTaskRecord(
        task_id="still-active", session_key=session.session_key, status=status, created_at=999,
    ))
    with pytest.raises(RpcHandlerError) as caught:
        await _handle_sessions_routing_set(params(session, None), ctx)
    assert caught.value.code == "SESSION_MODEL_BUSY"
    assert caught.value.retryable is True and caught.value.accepted is False
    assert (await manager.get_session(session.session_key)).model == "first"


@pytest.mark.asyncio
async def test_reserved_work_is_checked_inside_admission_gate(setup):
    _, _, session, ctx = setup
    inside = False
    @asynccontextmanager
    async def collect(_key):
        nonlocal inside
        inside = True
        try:
            yield
        finally:
            inside = False
    async def has_work(_key):
        assert inside
        return True
    ctx.task_runtime = SimpleNamespace(collect_admission=collect, has_session_work=has_work)
    with pytest.raises(RpcHandlerError) as caught:
        await _handle_sessions_routing_set(params(session, None), ctx)
    assert caught.value.code == "SESSION_MODEL_BUSY"
    assert inside is False


@pytest.mark.asyncio
async def test_legacy_turn_lock_returns_busy_without_waiting(setup):
    _, _, session, ctx = setup
    lock = asyncio.Lock()
    await lock.acquire()
    ctx.turn_runner = SimpleNamespace(get_session_lock=lambda _: lock)
    try:
        with pytest.raises(RpcHandlerError) as caught:
            await asyncio.wait_for(_handle_sessions_routing_set(params(session, None), ctx), 0.2)
        assert caught.value.code == "SESSION_MODEL_BUSY"
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_idle_switch_holds_admission_gate_through_atomic_write(setup):
    storage, manager, session, ctx = setup
    gate = asyncio.Lock()
    arrived = asyncio.Event()
    release = asyncio.Event()
    original = manager.set_session_routing
    @asynccontextmanager
    async def collect(_key):
        async with gate:
            yield
    async def delayed(*args, **kwargs):
        assert gate.locked()
        arrived.set()
        await release.wait()
        return await original(*args, **kwargs)
    manager.set_session_routing = delayed
    ctx.task_runtime = SimpleNamespace(
        collect_admission=collect, has_session_work=AsyncMock(return_value=False),
    )
    switch = asyncio.create_task(_handle_sessions_routing_set(
        params(session, {"model": "next", "provider": "openrouter"}), ctx,
    ))
    await arrived.wait()
    async def next_admission():
        async with collect(session.session_key):
            return await storage.get_session(session.session_key)
    send = asyncio.create_task(next_admission())
    await asyncio.sleep(0)
    assert not send.done()
    release.set()
    await switch
    accepted = await send
    assert (accepted.model, accepted.provider_override, accepted.model_routing_mode) == (
        "next", "openrouter", "direct",
    )
