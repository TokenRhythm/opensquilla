"""Send failures retain useful correlations without logging private payloads."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import cast

import pytest
from structlog.testing import capture_logs

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc.registry import (
    RpcContext,
    RpcHandlerError,
    RpcRegistry,
    RpcUnavailableError,
)
from opensquilla.observability.log_privacy import private_log_event
from opensquilla.session.storage import StorageBusyError

_SEND_METHODS = (
    "chat.send",
    "sessions.send",
    "sessions.steer.v2",
    "sessions.pending_inputs.enqueue",
    "sessions.pending_inputs.dispatch",
    "sessions.pending_inputs.steer",
)
_PRIVATE = "synthetic-private-prompt-and-token"
_REQUEST_ID = f"request:{_PRIVATE}\nnot-a-log-entry"
_CONNECTION_ID = f"connection:{_PRIVATE}"


@pytest.mark.parametrize("method", _SEND_METHODS)
@pytest.mark.parametrize(
    ("failure", "code", "accepted", "retryable"),
    [
        (
            RpcHandlerError(
                "ADMISSION_REJECTED", _PRIVATE, accepted=False, retryable=True,
                details={"token": _PRIVATE, "message": _PRIVATE},
            ),
            "ADMISSION_REJECTED", False, True,
        ),
        (
            RpcHandlerError("TASK_FAILED", _PRIVATE, accepted=True, retryable=False),
            "TASK_FAILED", True, False,
        ),
        (
            StorageBusyError(_PRIVATE, waited_ms=10, retry_after_ms=25, resource=_PRIVATE),
            "STORAGE_BUSY", None, True,
        ),
        (RpcUnavailableError(_PRIVATE), "UNAVAILABLE", None, True),
        (RuntimeError(_PRIVATE), "INTERNAL_ERROR", None, False),
    ],
    ids=("rejected", "accepted-failure", "storage-busy", "unavailable", "unexpected"),
)
async def test_send_failure_logs_one_safe_summary_without_changing_response(
    method: str, failure: Exception, code: str, accepted: bool | None, retryable: bool,
) -> None:
    registry = RpcRegistry()

    async def handler(params, ctx):
        raise failure

    registry.register(method, handler, "operator.write")
    with capture_logs(processors=[private_log_event]) as events:
        response = await registry.dispatch(
            _REQUEST_ID, method,
            {"message": _PRIVATE, "token": _PRIVATE, "attachments": [{"name": _PRIVATE}]},
            RpcContext(conn_id=_CONNECTION_ID),
        )

    assert response.ok is False
    assert response.id == _REQUEST_ID
    assert response.error is not None
    assert (response.error.code, response.error.accepted, response.error.retryable) == (
        code, accepted, retryable,
    )
    if isinstance(failure, RpcHandlerError):
        assert response.error.message == _PRIVATE
        assert response.error.details == failure.details
    assert events == [{
        "event": "rpc.send_failed", "log_level": "warning", "method": method,
        "request_id": "sha256:" + hashlib.sha256(_REQUEST_ID.encode()).hexdigest(),
        "connection_id": "sha256:" + hashlib.sha256(_CONNECTION_ID.encode()).hexdigest(),
        "code": code, "accepted": accepted, "retryable": retryable,
    }]
    assert _PRIVATE not in json.dumps(events)


@pytest.mark.parametrize("guest", [False, True], ids=("missing-scope", "foreign-guest-session"))
async def test_send_authorization_denials_are_logged_without_calling_handler(guest: bool) -> None:
    registry = RpcRegistry()
    called = False

    async def handler(params, ctx):
        nonlocal called
        called = True
        return {}

    method = "sessions.pending_inputs.enqueue"
    registry.register(method, handler, "operator.write")
    principal = Principal(
        role="operator", scopes=frozenset({"operator.read", "operator.write"} if guest else set()),
        authenticated=not guest, is_owner=not guest,
        auth_state="guest" if guest else "authenticated",
        capabilities=frozenset({"guest.safe"}) if guest else frozenset(),
        guest_owner_id="a" * 64 if guest else None,
    )
    with capture_logs(processors=[private_log_event]) as events:
        response = await registry.dispatch(
            _REQUEST_ID, method,
            {"key": "agent:main:webchat:owner", "message": _PRIVATE},
            RpcContext(conn_id=_CONNECTION_ID, principal=principal),
        )

    assert not called
    assert response.error is not None and response.error.code == "UNAUTHORIZED"
    assert len(events) == 1
    assert events[0]["code"] == "UNAUTHORIZED"
    assert events[0]["accepted"] is None
    assert _PRIVATE not in json.dumps(events)


async def test_invalid_send_envelope_logs_only_hashed_identifier() -> None:
    registry = RpcRegistry()
    request_id = f"{_PRIVATE}\ud800"
    with capture_logs(processors=[private_log_event]) as events:
        response = await registry.dispatch(request_id, "chat.send", {}, RpcContext(conn_id="test"))

    assert response.error is not None and response.error.code == "INVALID_REQUEST"
    assert len(events) == 1
    assert events[0]["request_id"] == "sha256:" + hashlib.sha256(
        request_id.encode("utf-8", errors="replace"),
    ).hexdigest()
    assert _PRIVATE not in json.dumps(events)


async def test_early_invalid_send_keeps_supporting_context_without_connection_id() -> None:
    with capture_logs(processors=[private_log_event]) as events:
        response = await RpcRegistry().dispatch(
            "\ud800", "chat.send", {}, cast(RpcContext, SimpleNamespace()),
        )

    assert response.error is not None and response.error.code == "INVALID_REQUEST"
    assert len(events) == 1
    assert events[0]["connection_id"] is None


async def test_arbitrary_error_code_cannot_inject_private_text_into_send_log() -> None:
    registry = RpcRegistry()

    async def handler(params, ctx):
        raise RpcHandlerError(f"ERROR\n{_PRIVATE}", _PRIVATE)

    registry.register("chat.send", handler, "operator.write")
    with capture_logs(processors=[private_log_event]) as events:
        response = await registry.dispatch("req", "chat.send", {}, RpcContext(conn_id="test"))

    assert response.error is not None and response.error.code == f"ERROR\n{_PRIVATE}"
    assert len(events) == 1 and events[0]["code"] == "UNKNOWN_ERROR"
    assert _PRIVATE not in json.dumps(events)


async def test_successful_send_and_non_send_rejection_do_not_emit_send_failures() -> None:
    registry = RpcRegistry()

    async def success(params, ctx):
        return {"accepted": True}

    async def rejection(params, ctx):
        raise RpcHandlerError("SYNTHETIC_REJECTION", _PRIVATE)

    registry.register("chat.send", success, "operator.write")
    registry.register("test.read", rejection, "operator.read")
    with capture_logs(processors=[private_log_event]) as events:
        successful = await registry.dispatch("ok", "chat.send", {}, RpcContext(conn_id="test"))
        rejected = await registry.dispatch("no", "test.read", {}, RpcContext(conn_id="test"))

    assert successful.ok and not rejected.ok
    assert events == []
