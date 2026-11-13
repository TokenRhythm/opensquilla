from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.scopes import ADMIN_SCOPE, METHOD_SCOPES, READ_SCOPE
from opensquilla.jev_service import JevService

SESSION = "agent:main:webchat:jev-test"


@pytest.fixture
async def setup_ctx(monkeypatch):
    config = GatewayConfig()
    service = JevService()
    session = SimpleNamespace(session_id="synthetic-id", epoch=0)
    storage = SimpleNamespace(_db_path=":memory:", get_session=AsyncMock(return_value=session))
    ctx = RpcContext(
        conn_id="jev-test", config=config,
        session_manager=SimpleNamespace(storage=storage),
        turn_runner=SimpleNamespace(jev_service=service),
    )
    emitter = AsyncMock()
    monkeypatch.setattr("opensquilla.gateway.rpc_sessions._emit_to_subscribers", emitter)
    monkeypatch.setattr(
        "opensquilla.engine.steps.jev_router.resolve_jev_deployment",
        lambda *args, **kwargs: SimpleNamespace(ready=True),
    )
    await service.bind_session(SESSION, session.session_id, session.epoch)
    yield ctx, service, emitter
    await service.close()


async def dispatch(method, params, ctx):
    return await get_dispatcher().dispatch("jev-request", method, params, ctx)


async def publish(service, revision=7):
    return await service.publish(SESSION, {
        "action": "switch", "current": {"provider": "tokenrhythm", "model": "large"},
        "target": {"provider": "tokenrhythm", "model": "small"},
        "reason": "A smaller model can classify these short synthetic labels.",
        "evidence": ["batch_task"], "risk": "low",
    }, input_id="synthetic-input", routing_revision=revision)


async def publish_handoff(service):
    return await service.publish(SESSION, {
        "action": "new_session", "current": {"provider": "tokenrhythm", "model": "large"},
        "target": None, "reason": "A summary can reduce the synthetic history.",
        "evidence": ["context_pressure"], "risk": "medium",
        "handoff": {"sourceSessionKey": SESSION, "contextTokens": 40_000,
                    "contextWindowTokens": 50_000},
    }, input_id="synthetic-context")


async def test_handoff_requires_idle_explicit_confirmation_and_releases_global_mutex(
    setup_ctx, monkeypatch,
) -> None:
    from opensquilla.gateway.rpc_jev import _handle_jev_respond

    ctx, service, _ = setup_ctx
    await service.configure(SESSION, {"enabled": True, "mode": "trust"}, ctx.config.jev_router)
    record = await publish_handoff(service)
    helper = AsyncMock()
    monkeypatch.setattr("opensquilla.gateway.jev_handoff.create_jev_handoff", helper)
    params = {"sessionKey": SESSION, "decisionId": record["id"], "accepted": True}
    await _handle_jev_respond(params, ctx, automatic=True)
    helper.assert_not_awaited()
    owner = object()
    service.claim_execution(SESSION, owner)
    result = await dispatch("jev.respond", params, ctx)
    assert not result.ok
    assert (await service.get_decision(SESSION, record["id"]))["status"] == "pending"
    helper.assert_not_awaited()
    service.release_execution(SESSION, owner)

    async def summarize(*_args):
        assert not service.response_lock.locked()
        assert (await service.get_decision(SESSION, record["id"]))["status"] == "accepted"
        return {"targetSessionKey": "synthetic:handoff-target"}

    helper.side_effect = summarize
    result = await dispatch("jev.respond", params, ctx)
    assert result.ok
    applied = result.payload["decisions"][0]
    assert applied["status"] == "applied"
    assert applied["handoff"]["targetSessionKey"] == "synthetic:handoff-target"
    assert (await dispatch("jev.respond", params, ctx)).ok
    assert helper.await_count == 1


@pytest.mark.parametrize("revoke,expected", [(False, "pending"), (True, "expired")])
async def test_failed_handoff_can_retry_without_reviving_revoked_decision(
    setup_ctx, monkeypatch, revoke, expected,
) -> None:
    ctx, service, _ = setup_ctx
    await service.configure(SESSION, {"enabled": True}, ctx.config.jev_router)
    record = await publish_handoff(service)

    async def fail(*_args):
        if revoke:
            await service.expire(SESSION, record["id"])
        raise RuntimeError("Synthetic summary failure")

    monkeypatch.setattr("opensquilla.gateway.jev_handoff.create_jev_handoff", fail)
    result = await dispatch("jev.respond", {
        "sessionKey": SESSION, "decisionId": record["id"], "accepted": True,
    }, ctx)
    assert not result.ok
    assert (await service.get_decision(SESSION, record["id"]))["status"] == expected


def test_methods_have_explicit_operator_scopes() -> None:
    assert METHOD_SCOPES["jev.get"] == READ_SCOPE
    assert METHOD_SCOPES["jev.configure"] == ADMIN_SCOPE
    assert METHOD_SCOPES["jev.respond"] == ADMIN_SCOPE
    assert METHOD_SCOPES["jev.preview"] == ADMIN_SCOPE


async def test_preview_acknowledges_before_inference_and_returns_adoptable_context(
    setup_ctx, monkeypatch,
) -> None:
    from opensquilla.engine.steps.jev_router import jev_input_fingerprint

    ctx, service, _ = setup_ctx
    await service.configure(SESSION, {"enabled": True}, ctx.config.jev_router)
    monkeypatch.setattr(
        "opensquilla.engine.steps.jev_router.resolve_jev_deployment",
        lambda *_args, **_kwargs: SimpleNamespace(
            ready=True, provider_config=SimpleNamespace(provider="tokenrhythm", model="large"),
        ),
    )
    started = asyncio.Event()
    finished = asyncio.Event()

    async def evaluate(turn, **_kwargs):
        started.set()
        await finished.wait()
        turn.metadata["jev_decision_id"] = "synthetic-decision"
        return turn

    monkeypatch.setattr("opensquilla.engine.steps.jev_router.apply_jev_router", evaluate)
    text = "Sort these synthetic labels alphabetically."
    result = await asyncio.wait_for(dispatch("jev.preview", {
        "sessionKey": SESSION, "draftId": "draft-1", "text": text,
    }, ctx), 1)
    assert result.ok
    assert result.payload["draftId"] == "draft-1"
    assert result.payload["status"] == "checking"
    await asyncio.wait_for(started.wait(), 1)
    task = service.current_check(SESSION, jev_input_fingerprint(text))
    assert task is not None and not task.done()
    finished.set()
    assert (await task).metadata["jev_decision_id"] == "synthetic-decision"


async def test_empty_preview_invalidates_old_draft_and_running_owner_is_preserved(
    setup_ctx,
) -> None:
    ctx, service, _ = setup_ctx
    await service.configure(SESSION, {"enabled": True}, ctx.config.jev_router)
    entered = asyncio.Event()

    async def check(_generation):
        entered.set()
        await asyncio.Event().wait()

    task = await service.start_check(SESSION, "old-draft", check)
    await entered.wait()
    result = await dispatch("jev.preview", {
        "sessionKey": SESSION, "draftId": "invalidated", "text": "",
    }, ctx)
    assert result.ok
    assert task.cancelled()
    assert result.payload["draftId"] == "invalidated"
    assert result.payload["status"] == "ready"
    owned = object()
    service.claim_execution(SESSION, owned)
    task = await service.start_check(SESSION, "submitted", check)
    result = await dispatch("jev.preview", {
        "sessionKey": SESSION, "draftId": "late-clear", "text": "",
    }, ctx)
    assert result.ok
    assert service.current_check(SESSION, "submitted") is task
    service.release_execution(SESSION, owned)


async def test_running_accept_is_resolved_by_execution_owner_without_idle_setter(
    setup_ctx, monkeypatch,
) -> None:
    ctx, service, _ = setup_ctx
    record = await publish(service)
    service.claim_execution(SESSION, object())
    setter = AsyncMock(side_effect=AssertionError("Idle setter cannot run mid-turn"))
    monkeypatch.setattr("opensquilla.gateway.rpc_sessions._handle_sessions_routing_set", setter)
    result = await dispatch("jev.respond", {
        "sessionKey": SESSION, "decisionId": record["id"], "accepted": True,
    }, ctx)
    assert result.ok
    assert result.payload["decisions"][0]["status"] == "accepted"
    setter.assert_not_awaited()


async def test_running_switch_notification_uses_canonical_routing_event(
    setup_ctx, monkeypatch,
) -> None:
    ctx, service, emitter = setup_ctx
    await dispatch("jev.get", {"sessionKey": SESSION}, ctx)
    routing = {
        "key": SESSION, "sessionKey": SESSION, "mode": "direct", "revision": 8,
        "modelSelection": {"provider": "tokenrhythm", "model": "small"},
    }
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._handle_sessions_routing_get",
        AsyncMock(return_value=routing),
    )
    await service.notify_routing_change(SESSION)
    assert emitter.await_args.args[2:] == ("sessions.routing.changed", routing)


async def test_configuration_updates_preferences_and_emits_snapshot(setup_ctx) -> None:
    ctx, service, emitter = setup_ctx
    result = await dispatch("jev.configure", {
        "sessionKey": SESSION, "enabled": True, "mode": "trust", "budgetPerHour": 2,
    }, ctx)
    assert result.ok is True
    assert result.payload["enabled"] is True
    assert result.payload["mode"] == "trust"
    assert result.payload["budgetPerHour"] == 2
    assert service.is_enabled(SESSION, ctx.config.jev_router)
    assert emitter.await_args.args[2] == "jev.changed"


async def test_accept_uses_atomic_session_routing_and_retry_is_idempotent(
    setup_ctx, monkeypatch,
) -> None:
    ctx, service, _ = setup_ctx
    decision = await publish(service)
    get_routing = AsyncMock(return_value={"revision": 7})
    set_routing = AsyncMock(return_value={"revision": 8})
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._handle_sessions_routing_get", get_routing,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._handle_sessions_routing_set", set_routing,
    )
    params = {"sessionKey": SESSION, "decisionId": decision["id"], "accepted": True}
    result = await dispatch("jev.respond", params, ctx)
    assert result.ok is True
    assert result.payload["decisions"][0]["status"] == "applied"
    assert await service.routing_revision(SESSION, decision["id"]) == 8
    assert set_routing.await_args.args[0] == {
        "sessionKey": SESSION, "mode": "direct", "expectedRevision": 7,
        "modelSelection": {"provider": "tokenrhythm", "model": "small"},
    }
    assert (await dispatch("jev.respond", params, ctx)).ok is True
    assert set_routing.await_count == 1


async def test_stale_recommendation_expires_without_overriding_new_manual_model(
    setup_ctx, monkeypatch,
) -> None:
    ctx, service, _ = setup_ctx
    decision = await publish(service, revision=6)
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._handle_sessions_routing_get",
        AsyncMock(return_value={"revision": 7}),
    )
    setter = AsyncMock()
    monkeypatch.setattr("opensquilla.gateway.rpc_sessions._handle_sessions_routing_set", setter)
    result = await dispatch("jev.respond", {
        "sessionKey": SESSION, "decisionId": decision["id"], "accepted": True,
    }, ctx)
    assert result.ok is False
    assert result.error.code == "CONFLICT"
    assert (await service.get_decision(SESSION, decision["id"]))["status"] == "expired"
    setter.assert_not_awaited()


async def test_busy_session_keeps_recommendation_retryable(setup_ctx, monkeypatch) -> None:
    ctx, service, _ = setup_ctx
    decision = await publish(service)
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._handle_sessions_routing_get",
        AsyncMock(return_value={"revision": 7}),
    )
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._handle_sessions_routing_set",
        AsyncMock(side_effect=RpcHandlerError("SESSION_MODEL_BUSY", "Wait for the current turn.")),
    )
    result = await dispatch("jev.respond", {
        "sessionKey": SESSION, "decisionId": decision["id"], "accepted": True,
    }, ctx)
    assert result.ok is False
    assert result.error.code == "SESSION_MODEL_BUSY"
    assert (await service.get_decision(SESSION, decision["id"]))["status"] == "pending"


async def test_dismiss_is_persisted_and_read_principals_cannot_mutate(setup_ctx) -> None:
    ctx, service, _ = setup_ctx
    decision = await publish(service)
    result = await dispatch("jev.respond", {
        "sessionKey": SESSION, "decisionId": decision["id"], "accepted": False,
    }, ctx)
    assert result.ok is True
    assert result.payload["recommendation"] is None
    assert result.payload["decisions"][0]["status"] == "dismissed"
    ctx.principal = Principal(
        role="operator", scopes=frozenset({READ_SCOPE}), is_owner=False, authenticated=True,
    )
    assert (await dispatch("jev.get", {"sessionKey": SESSION}, ctx)).ok is True
    denied = await dispatch("jev.configure", {"sessionKey": SESSION, "enabled": True}, ctx)
    assert denied.ok is False


@pytest.mark.parametrize("mode", ["suggest", "trust"])
@pytest.mark.parametrize("running", [False, True])
async def test_deadline_uses_mode_for_idle_and_running_sessions(
    setup_ctx, monkeypatch, mode, running,
) -> None:
    ctx, service, _ = setup_ctx
    await service.configure(SESSION, {"enabled": True, "mode": mode}, ctx.config.jev_router)
    now = [100_000]
    service._clock = lambda: now[0]
    record = await publish(service)
    await dispatch("jev.get", {"sessionKey": SESSION}, ctx)
    setter = AsyncMock(return_value={"revision": 8})
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._handle_sessions_routing_get",
        AsyncMock(return_value={"revision": 7}),
    )
    monkeypatch.setattr("opensquilla.gateway.rpc_sessions._handle_sessions_routing_set", setter)
    if running:
        service.claim_execution(SESSION, "synthetic-turn")
    now[0] = record["autoSwitchAt"] + 1
    await asyncio.wait_for(service.settle_deadline(SESSION, record["id"]), 1)
    updated = await service.get_decision(SESSION, record["id"])
    assert updated["status"] == (
        "dismissed" if mode == "suggest" else "accepted" if running else "applied"
    )
    assert setter.await_count == (1 if mode == "trust" and not running else 0)
    await service.settle_deadline(SESSION, record["id"])
    assert setter.await_count == (1 if mode == "trust" and not running else 0)


async def test_deadline_rechecks_global_mode_before_automatic_authorization(
    setup_ctx, monkeypatch,
) -> None:
    ctx, service, _ = setup_ctx
    ctx.config.jev_router.enabled = True
    ctx.config.jev_router.mode = "trust"
    now = [100_000]
    service._clock = lambda: now[0]
    record = await publish(service)
    await dispatch("jev.get", {"sessionKey": SESSION}, ctx)
    ctx.config.jev_router.mode = "suggest"
    setter = AsyncMock()
    monkeypatch.setattr("opensquilla.gateway.rpc_sessions._handle_sessions_routing_set", setter)
    now[0] = record["autoSwitchAt"] + 1
    await service.settle_deadline(SESSION, record["id"])
    assert (await service.get_decision(SESSION, record["id"]))["status"] == "dismissed"
    setter.assert_not_awaited()


async def test_reset_session_cannot_accept_old_generation_decision(setup_ctx, monkeypatch) -> None:
    ctx, service, _ = setup_ctx
    decision = await publish(service)
    ctx.session_manager.storage.get_session.return_value.epoch = 1
    setter = AsyncMock()
    monkeypatch.setattr("opensquilla.gateway.rpc_sessions._handle_sessions_routing_set", setter)
    result = await dispatch("jev.respond", {
        "sessionKey": SESSION, "decisionId": decision["id"], "accepted": True,
    }, ctx)
    assert result.ok is False
    setter.assert_not_awaited()
    assert await service.get_decision(SESSION, decision["id"]) is None
