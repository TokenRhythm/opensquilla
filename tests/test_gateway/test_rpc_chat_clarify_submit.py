"""Structured clarification resolves the original request without a new turn."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.gateway import rpc_chat as rpc_chat_module
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc.registry import RpcRegistry
from opensquilla.gateway.rpc_chat import (
    _submit_clarification,
)
from opensquilla.gateway.user_input_broker import StructuredUserInputBroker

# ── pure helper ──


# ── RPC handler ──

@pytest.mark.asyncio
async def test_clarify_submit_rejects_non_dict_params():
    ctx = RpcContext(conn_id="c", principal=SimpleNamespace(role="operator"))
    with pytest.raises(ValueError, match="sessionKey, fields"):
        await _submit_clarification(None, ctx)
    with pytest.raises(ValueError, match="sessionKey, fields"):
        await _submit_clarification("not-a-dict", ctx)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_clarify_submit_rejects_empty_fields():
    ctx = RpcContext(conn_id="c", principal=SimpleNamespace(role="operator"))
    with pytest.raises(ValueError, match="non-empty mapping"):
        await _submit_clarification(
            {"sessionKey": "S1", "fields": {}}, ctx,
        )
    with pytest.raises(ValueError, match="non-empty mapping"):
        await _submit_clarification(
            {"sessionKey": "S1", "fields": "not a dict"}, ctx,
        )


@pytest.mark.asyncio
async def test_clarify_submit_with_request_id_resolves_same_turn(monkeypatch):
    captured: dict = {}

    class Runtime:
        async def resolve_user_input(self, **kwargs):
            captured.update(kwargs)
            return {
                "resolved": True,
                "replayed": False,
                "request_id": kwargs["request_id"],
            }

    async def _unexpected_send(*_args, **_kwargs):
        raise AssertionError("deferred input must not create a new chat turn")

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_chat._handle_chat_send",
        _unexpected_send,
    )
    ctx = RpcContext(
        conn_id="c",
        principal=SimpleNamespace(role="operator"),
        task_runtime=Runtime(),
    )

    result = await _submit_clarification(
        {
            "sessionKey": "agent:main:webchat:abc",
            "request_id": "input-1",
            "fields": {"scope": "Core"},
        },
        ctx,
    )

    assert result == {
        "sessionKey": "agent:main:webchat:abc",
        "resolved": True,
        "replayed": False,
        "request_id": "input-1",
    }
    assert captured == {
        "session_key": "agent:main:webchat:abc",
        "request_id": "input-1",
        "fields": {"scope": "Core"},
    }


def _deferred_rpc_harness(monkeypatch):
    broker = StructuredUserInputBroker()
    public = broker.open_request(
        session_key="agent:main:webchat:clarify-test",
        task_id="clarify-task",
        tool_use_id="clarify-tool",
        payload={
            "clarify_schema": {
                "fields": [{
                    "name": "scope",
                    "type": "enum",
                    "required": True,
                    "choices": ["Core", "Full"],
                }],
            },
        },
    )

    def unexpected_admission(_ctx):
        pytest.fail("a request-scoped clarification must not admit another turn")

    monkeypatch.setattr(rpc_chat_module, "_chat_turn_admission_adapter", unexpected_admission)

    async def resolve_user_input(**kwargs):
        return broker.resolve(**kwargs)

    ctx = RpcContext(
        conn_id="clarify-test",
        task_runtime=SimpleNamespace(resolve_user_input=resolve_user_input),
    )
    registry = RpcRegistry()
    registry.register(
        "chat.clarify_submit",
        rpc_chat_module._handle_chat_clarify_submit_generated_contract,
        "operator.write",
    )
    params = {
        "sessionKey": "agent:main:webchat:clarify-test",
        "requestId": public["request_id"],
        "fields": {"scope": "Core"},
    }
    return broker, registry, ctx, params


@pytest.mark.asyncio
async def test_deferred_clarify_rpc_resolves_live_broker_and_replays_without_new_turn(monkeypatch):
    broker, registry, ctx, params = _deferred_rpc_harness(monkeypatch)

    response = await registry.dispatch("answer", "chat.clarify_submit", params, ctx)

    assert response.ok is True
    assert response.payload["resolved"] is True
    assert response.payload["replayed"] is False
    assert await broker.wait_for_response(params["requestId"]) == {"scope": "Core"}
    replay = await registry.dispatch("retry", "chat.clarify_submit", params, ctx)
    assert replay.ok is True
    assert replay.payload["replayed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True], ids=["unknown", "cancelled"])
async def test_deferred_clarify_rpc_expires_missing_request_without_new_turn(
    monkeypatch, cancelled,
):
    broker, registry, ctx, params = _deferred_rpc_harness(monkeypatch)
    if cancelled:
        broker.cancel_task("clarify-task")
    else:
        params["requestId"] = "missing-request"

    response = await registry.dispatch("expired", "chat.clarify_submit", params, ctx)

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "USER_INPUT_EXPIRED"
    assert response.error.retryable is False
    assert response.error.accepted is False
    assert response.error.details is None
    assert params["requestId"] not in response.error.message
    assert params["sessionKey"] not in response.error.message
    broker.cancel_task("clarify-task")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_owner", [False, True], ids=["validation", "ownership"])
async def test_deferred_clarify_rpc_preserves_invalid_request_and_pending_question(
    monkeypatch, invalid_owner,
):
    broker, registry, ctx, params = _deferred_rpc_harness(monkeypatch)
    invalid = dict(params)
    if invalid_owner:
        invalid["sessionKey"] = "agent:main:webchat:other"
    else:
        invalid["fields"] = {"scope": "not-offered"}

    response = await registry.dispatch("invalid", "chat.clarify_submit", invalid, ctx)

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "INVALID_REQUEST"
    assert len(broker.pending_for_session(params["sessionKey"])) == 1
    valid = await registry.dispatch("valid", "chat.clarify_submit", params, ctx)
    assert valid.ok is True
    assert await broker.wait_for_response(params["requestId"]) == {"scope": "Core"}


@pytest.mark.asyncio
async def test_clarification_requires_a_current_request_id():
    ctx = RpcContext(conn_id="c", principal=SimpleNamespace(role="operator"))
    with pytest.raises(ValueError, match="request_id"):
        await _submit_clarification(
            {"sessionKey": "S1", "fields": {"choice": "continue"}}, ctx,
        )
