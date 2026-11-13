from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.rpc_jev import _handle_jev_respond
from opensquilla.jev_service import JevService

SESSION = "agent:main:webchat:handoff-concurrency"
TARGET = "agent:main:webchat:synthetic-handoff-target"


@pytest.fixture
async def handoff_rpc(monkeypatch):
    config = GatewayConfig()
    service = JevService()
    storage = SimpleNamespace(get_session=AsyncMock(return_value=SimpleNamespace(
        session_id="synthetic-handoff-source", epoch=0,
    )))
    ctx = RpcContext(
        conn_id="synthetic-handoff-connection", config=config,
        session_manager=SimpleNamespace(storage=storage),
        turn_runner=SimpleNamespace(jev_service=service),
        task_runtime=SimpleNamespace(has_session_work=AsyncMock(return_value=False)),
    )
    monkeypatch.setattr("opensquilla.gateway.rpc_sessions._emit_to_subscribers", AsyncMock())
    monkeypatch.setattr(
        "opensquilla.engine.steps.jev_router.resolve_jev_deployment",
        lambda *args, **kwargs: SimpleNamespace(ready=True),
    )
    await service.bind_session(SESSION, "synthetic-handoff-source", 0)
    await service.configure(SESSION, {"enabled": True}, config.jev_router)
    notice = await service.publish(SESSION, {
        "action": "new_session", "current": {"provider": "openai", "model": "synthetic"},
        "target": None, "reason": "Continue a synthetic task with less history.",
        "evidence": ["context_pressure"], "risk": "medium",
        "handoff": {"sourceSessionKey": SESSION, "contextTokens": 40_000,
                    "contextWindowTokens": 50_000},
    })
    params = {"sessionKey": SESSION, "decisionId": notice["id"], "accepted": True}
    yield SimpleNamespace(ctx=ctx, service=service, params=params, decision_id=notice["id"])
    await service.close()


async def test_concurrent_accepts_wait_for_one_complete_handoff(handoff_rpc, monkeypatch):
    env = handoff_rpc
    started, finish = asyncio.Event(), asyncio.Event()

    async def create(*args):
        assert not env.service.response_lock.locked()
        started.set()
        await finish.wait()
        return {"targetSessionKey": TARGET}

    helper = AsyncMock(side_effect=create)
    monkeypatch.setattr("opensquilla.gateway.jev_handoff.create_jev_handoff", helper)
    first = asyncio.create_task(_handle_jev_respond(env.params, env.ctx))
    await started.wait()
    second = asyncio.create_task(_handle_jev_respond(env.params, env.ctx))
    await asyncio.sleep(0)
    assert helper.await_count == 1
    finish.set()
    snapshots = await asyncio.gather(first, second)
    for snapshot in snapshots:
        applied = next(item for item in snapshot["decisions"] if item["id"] == env.decision_id)
        assert applied["status"] == "applied"
        assert applied["handoff"]["targetSessionKey"] == TARGET
    assert helper.await_count == 1
    assert env.service._handoff_response_locks == {}


async def test_cancelling_a_waiter_does_not_revoke_the_active_handoff(handoff_rpc, monkeypatch):
    env = handoff_rpc
    started, finish = asyncio.Event(), asyncio.Event()

    async def create(*args):
        started.set()
        await finish.wait()
        return {"targetSessionKey": TARGET}

    monkeypatch.setattr("opensquilla.gateway.jev_handoff.create_jev_handoff", create)
    first = asyncio.create_task(_handle_jev_respond(env.params, env.ctx))
    await started.wait()
    waiter = asyncio.create_task(_handle_jev_respond(env.params, env.ctx))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert (await env.service.get_decision(SESSION, env.decision_id))["status"] == "accepted"
    finish.set()
    await first
    assert (await env.service.get_decision(SESSION, env.decision_id))["status"] == "applied"
    assert env.service._handoff_response_locks == {}


async def test_cancelling_summary_reopens_its_approval_for_retry(handoff_rpc, monkeypatch):
    env = handoff_rpc
    started = asyncio.Event()

    async def create(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("opensquilla.gateway.jev_handoff.create_jev_handoff", create)
    operation = asyncio.create_task(_handle_jev_respond(env.params, env.ctx))
    await started.wait()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert (await env.service.get_decision(SESSION, env.decision_id))["status"] == "pending"
    assert env.service._handoff_response_locks == {}


async def test_cancelled_claim_settles_before_cleanup(handoff_rpc, monkeypatch):
    env = handoff_rpc
    claimed, finish = asyncio.Event(), asyncio.Event()
    original = env.service.respond

    async def claim(*args):
        result = await original(*args)
        claimed.set()
        await finish.wait()
        return result

    helper = AsyncMock()
    monkeypatch.setattr(env.service, "respond", claim)
    monkeypatch.setattr("opensquilla.gateway.jev_handoff.create_jev_handoff", helper)
    operation = asyncio.create_task(_handle_jev_respond(env.params, env.ctx))
    await claimed.wait()
    operation.cancel()
    await asyncio.sleep(0)
    assert env.service.response_lock.locked()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    helper.assert_not_awaited()
    assert (await env.service.get_decision(SESSION, env.decision_id))["status"] == "pending"
    assert env.service._handoff_response_locks == {}


async def test_rejection_during_summary_is_immediate_and_cannot_be_reopened(
    handoff_rpc, monkeypatch,
):
    env = handoff_rpc
    started, finish = asyncio.Event(), asyncio.Event()

    async def create(*args):
        started.set()
        await finish.wait()
        return {"targetSessionKey": TARGET}

    monkeypatch.setattr("opensquilla.gateway.jev_handoff.create_jev_handoff", create)
    operation = asyncio.create_task(_handle_jev_respond(env.params, env.ctx))
    await started.wait()
    await asyncio.wait_for(_handle_jev_respond({**env.params, "accepted": False}, env.ctx), 1)
    assert (await env.service.get_decision(SESSION, env.decision_id))["status"] == "dismissed"
    finish.set()
    with pytest.raises(RpcHandlerError) as caught:
        await operation
    assert caught.value.code == "CONFLICT"
    assert (await env.service.get_decision(SESSION, env.decision_id))["status"] == "dismissed"
    assert env.service._handoff_response_locks == {}
