from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.sidechat_leases import _leases, renew_sidechat_lease
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus, SessionSummary
from opensquilla.session.sidechat import set_state
from opensquilla.session.storage import SessionStorage

PARENT = "agent:main:webchat:side-parent"


@pytest_asyncio.fixture
async def ctx():
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    locks = defaultdict(asyncio.Lock)
    context = RpcContext(
        conn_id="test-sidechat",
        principal=Principal(
            role="operator", scopes=frozenset({"operator.admin"}), is_owner=True, authenticated=True
        ),
        config=GatewayConfig(memory={}),
    )
    context.session_manager = manager
    context.turn_runner = SimpleNamespace(get_session_lock=lambda key: locks[key])
    await manager.create(PARENT, model="parent-model")
    await manager.append_message(PARENT, "user", "Parent reference")
    yield context
    tasks = [task for (owner, _), task in list(_leases.items()) if owner == id(storage)]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await storage.close()


@pytest.mark.asyncio
async def test_create_authoritative_contract_idempotency_and_owner(ctx):
    dispatcher = get_dispatcher()
    params = {"parentKey": PARENT, "clientRequestId": "owner-panel"}
    response = await dispatcher.dispatch("create", "sessions.sidechat.create", params, ctx)
    assert response.ok, response.error
    assert response.payload["inherited"]["model"] == "parent-model"
    repeated = await dispatcher.dispatch("retry", "sessions.sidechat.create", params, ctx)
    assert repeated.payload["key"] == response.payload["key"]
    ctx.principal = Principal(
        role="operator", scopes=frozenset({"operator.admin"}), is_owner=False, authenticated=True
    )
    denied = await dispatcher.dispatch("denied", "sessions.sidechat.create", params, ctx)
    assert denied.error.code == "OWNER_REQUIRED"


@pytest.mark.asyncio
async def test_explicit_close_fences_admission_and_cancels_exact_child(ctx, monkeypatch):
    child = await ctx.session_manager.create_sidechat(PARENT, "close-panel")
    cancel = AsyncMock(return_value={"aborted": True})
    monkeypatch.setattr(
        rpc_sessions, "_session_turn_admission_adapter", lambda _: SimpleNamespace(cancel=cancel)
    )
    result = await rpc_sessions._handle_sessions_sidechat_close({"key": child.session_key}, ctx)
    assert result == {"key": child.session_key, "closed": True}
    cancel.assert_awaited_once_with({"key": child.session_key}, surface="session")
    node = await ctx.session_manager._storage.get_session(child.session_key)
    assert node.origin["sidechat"]["state"] == "closed"
    await rpc_sessions._handle_sessions_sidechat_close({"key": child.session_key}, ctx)
    assert cancel.await_count == 1
    assert await ctx.session_manager.get_transcript(PARENT)
    assert await ctx.session_manager.get_transcript(child.session_key)


@pytest.mark.asyncio
async def test_close_does_not_acknowledge_while_durable_task_still_active(ctx, monkeypatch):
    child = await ctx.session_manager.create_sidechat(PARENT, "slow-close")
    await ctx.session_manager._storage.create_agent_task(
        AgentTaskRecord(
            task_id="still-running",
            session_key=child.session_key,
            status=AgentTaskStatus.RUNNING,
        )
    )
    monkeypatch.setattr(
        rpc_sessions,
        "_session_turn_admission_adapter",
        lambda _: SimpleNamespace(cancel=AsyncMock()),
    )
    response = await get_dispatcher().dispatch(
        "close",
        "sessions.sidechat.close",
        {"key": child.session_key},
        ctx,
    )
    assert response.error.code == "UNAVAILABLE"
    node = await ctx.session_manager._storage.get_session(child.session_key)
    assert node.origin["sidechat"]["state"] == "closing"


@pytest.mark.asyncio
async def test_close_keeps_uncertain_cancellation_fenced_even_without_active_ledger(
    ctx, monkeypatch,
):
    child = await ctx.session_manager.create_sidechat(PARENT, "uncertain-close")
    monkeypatch.setattr(
        rpc_sessions, "_session_turn_admission_adapter",
        lambda _: SimpleNamespace(cancel=AsyncMock(return_value={
            "aborted": False, "reason": "task_cancel_unknown",
        })),
    )
    response = await get_dispatcher().dispatch(
        "close", "sessions.sidechat.close", {"key": child.session_key}, ctx,
    )
    assert response.error.code == "UNAVAILABLE"
    node = await ctx.session_manager._storage.get_session(child.session_key)
    assert node.origin["sidechat"]["state"] == "closing"


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_only", [False, True])
async def test_close_waits_for_owned_subagent_without_cancelling_parent(
    ctx, monkeypatch, runtime_only,
):
    child = await ctx.session_manager.create_sidechat(PARENT, "worker-close")
    worker = await ctx.session_manager.create(
        "agent:main:subagent:side-worker", spawned_by=child.session_key,
        parent_session_key=child.session_key,
    )
    if runtime_only:
        ctx.task_runtime = SimpleNamespace(
            has_session_work=AsyncMock(side_effect=lambda key: key == worker.session_key),
        )
    else:
        await ctx.session_manager._storage.create_agent_task(AgentTaskRecord(
            task_id="worker-running", session_key=worker.session_key,
            status=AgentTaskStatus.RUNNING,
        ))
    cancel = AsyncMock(return_value={"aborted": True})
    monkeypatch.setattr(
        rpc_sessions, "_session_turn_admission_adapter", lambda _: SimpleNamespace(cancel=cancel),
    )
    response = await get_dispatcher().dispatch(
        "close", "sessions.sidechat.close", {"key": child.session_key}, ctx,
    )
    assert response.error.code == "UNAVAILABLE"
    node = await ctx.session_manager._storage.get_session(child.session_key)
    assert node.origin["sidechat"]["state"] == "closing"
    cancel.assert_awaited_once_with({"key": child.session_key}, surface="session")
    assert await ctx.session_manager.get_transcript(PARENT)


@pytest.mark.asyncio
async def test_close_holds_driver_fence_for_only_owned_subtree(ctx, monkeypatch):
    child = await ctx.session_manager.create_sidechat(PARENT, "driver-fence")
    worker = await ctx.session_manager.create(
        "agent:main:subagent:driver-worker", spawned_by=child.session_key,
        parent_session_key=child.session_key,
    )
    fenced = []

    @asynccontextmanager
    async def quiesce(keys, **kwargs):
        fenced.extend(keys)
        yield
        node = await ctx.session_manager._storage.get_session(child.session_key)
        assert node.origin["sidechat"]["state"] == "closed"

    ctx.task_runtime = SimpleNamespace(quiesce_sessions=quiesce)
    monkeypatch.setattr(
        rpc_sessions, "_session_turn_admission_adapter",
        lambda _: SimpleNamespace(cancel=AsyncMock(return_value={"aborted": True})),
    )
    result = await rpc_sessions._handle_sessions_sidechat_close({"key": child.session_key}, ctx)
    assert result["closed"] is True
    assert set(fenced) == {child.session_key, worker.session_key}
    assert PARENT not in fenced


@pytest.mark.asyncio
async def test_close_driver_timeout_keeps_child_closing(ctx, monkeypatch):
    child = await ctx.session_manager.create_sidechat(PARENT, "driver-timeout")

    @asynccontextmanager
    async def quiesce(keys, **kwargs):
        await asyncio.Event().wait()
        yield

    ctx.task_runtime = SimpleNamespace(quiesce_sessions=quiesce)
    monkeypatch.setattr(rpc_sessions, "_ABORT_RUNTIME_CANCEL_DRAIN_SECONDS", 0.01)
    monkeypatch.setattr(
        rpc_sessions, "_session_turn_admission_adapter",
        lambda _: SimpleNamespace(cancel=AsyncMock(return_value={"aborted": True})),
    )
    response = await get_dispatcher().dispatch(
        "close", "sessions.sidechat.close", {"key": child.session_key}, ctx,
    )
    assert response.error.code == "UNAVAILABLE"
    node = await ctx.session_manager._storage.get_session(child.session_key)
    assert node.origin["sidechat"]["state"] == "closing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [
        "goals.set",
        "sessions.fork",
        "sessions.reset",
        "sessions.truncate",
        "sessions.sidechat.create",
        "plans.setMode",
        "plans.implement",
    ],
)
async def test_sidechat_rejects_goal_fork_and_destructive_parent_controls(ctx, method):
    child = await ctx.session_manager.create_sidechat(PARENT, "policy")
    params = (
        {"parentKey": child.session_key, "clientRequestId": "nested"}
        if method == "sessions.sidechat.create"
        else {"key": child.session_key}
    )
    response = await get_dispatcher().dispatch("policy", method, params, ctx)
    assert response.error.code == "INVALID_REQUEST"
    assert response.error.accepted is False
    assert "side conversation" in response.error.message


@pytest.mark.asyncio
async def test_orphan_owner_lease_cancels_and_cannot_be_revived(ctx, monkeypatch):
    import opensquilla.session.sidechat as sidechat

    child = await ctx.session_manager.create_sidechat(PARENT, "lease")
    monkeypatch.setattr(sidechat, "SIDECHAT_LEASE_MS", 20)
    closed = asyncio.Event()

    async def close():
        await set_state(ctx.session_manager._storage, child.session_key, "closed")
        closed.set()

    await renew_sidechat_lease(ctx.session_manager._storage, child.session_key, close)
    await asyncio.wait_for(closed.wait(), timeout=2)
    with pytest.raises(ValueError, match="closed"):
        await renew_sidechat_lease(ctx.session_manager._storage, child.session_key, close)


@pytest.mark.asyncio
async def test_inherited_summaries_are_model_only(ctx):
    from opensquilla.gateway.adapters.session_history_projection import read_chat_history_v4

    parent = await ctx.session_manager.get_session(PARENT)
    await ctx.session_manager._storage.save_summary(
        SessionSummary(
            session_id=parent.session_id,
            session_key=PARENT,
            summary_text="Parent summary",
        )
    )
    child = await ctx.session_manager.create_sidechat(PARENT, "summarized")
    history = await read_chat_history_v4({"sessionKey": child.session_key}, ctx)
    assert history["messages"] == []
    assert history["compaction_summaries"] == []
    assert history["history_scope"] == "complete"
    assert history["canonical_available"] is True
    assert history["canonical_complete"] is True
    assert (await ctx.session_manager.get_summaries(child.session_key))[
        0
    ].summary_text == "Parent summary"


@pytest.mark.asyncio
async def test_gateway_restart_restores_existing_deadline_without_renewing(ctx, monkeypatch):
    import opensquilla.session.sidechat as sidechat
    from opensquilla.gateway.sidechat_leases import restore_sidechat_leases

    monkeypatch.setattr(sidechat, "SIDECHAT_LEASE_MS", 60)
    child = await ctx.session_manager.create_sidechat(PARENT, "restart")
    original = child.origin["sidechat"]["leaseExpiresAt"]
    cancelled = asyncio.Event()

    async def close(key):
        await set_state(ctx.session_manager._storage, key, "closed")
        cancelled.set()

    await restore_sidechat_leases(ctx, close)
    await asyncio.wait_for(cancelled.wait(), timeout=2)
    node = await ctx.session_manager._storage.get_session(child.session_key)
    assert node.origin["sidechat"]["leaseExpiresAt"] == original
    assert node.origin["sidechat"]["state"] == "closed"


@pytest.mark.asyncio
async def test_restart_defers_transient_expired_child_cleanup_without_failing_boot(ctx):
    from opensquilla.gateway.sidechat_leases import restore_sidechat_leases

    child = await ctx.session_manager.create_sidechat(PARENT, "expired-draining")
    child.origin["sidechat"]["leaseExpiresAt"] = 1
    await ctx.session_manager._storage.upsert_session(child)
    attempts = 0
    cancelled = asyncio.Event()

    async def close(key):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("cleanup is still draining")
        await set_state(ctx.session_manager._storage, key, "closed")
        cancelled.set()

    await restore_sidechat_leases(ctx, close)
    node = await ctx.session_manager._storage.get_session(child.session_key)
    assert node.origin["sidechat"]["state"] in {"closing", "closed"}
    await asyncio.wait_for(cancelled.wait(), timeout=2)
    assert attempts == 2
    node = await ctx.session_manager._storage.get_session(child.session_key)
    assert node.origin["sidechat"]["leaseExpiresAt"] == 1
    assert node.origin["sidechat"]["state"] == "closed"
