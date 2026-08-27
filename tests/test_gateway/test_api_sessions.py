from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from starlette.testclient import TestClient

import opensquilla.gateway.app as gateway_app
from opensquilla.gateway.config import GatewayConfig


class _FakeDispatchResult:
    ok = True
    payload = {"sessions": [], "count": 0, "ts": 123}
    error = None


class _FakeDispatcher:
    def __init__(self, result: object | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, object] | None, object]] = []
        self.result = result or _FakeDispatchResult()

    async def dispatch(
        self,
        request_id: str,
        method: str,
        params: dict[str, object] | None,
        ctx: object,
    ) -> object:
        self.calls.append((request_id, method, params, ctx))
        return self.result


def _cursor_token(payload: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")


def test_api_sessions_forwards_pagination_query_params() -> None:
    dispatcher = _FakeDispatcher()

    original = gateway_app.get_dispatcher
    gateway_app.get_dispatcher = lambda: dispatcher
    try:
        app = gateway_app.create_gateway_app(GatewayConfig())
    finally:
        gateway_app.get_dispatcher = original

    with TestClient(app) as client:
        response = client.get(
            "/api/sessions?limit=200&view=session-list-v1&cursor=opaque-cursor"
        )

    assert response.status_code == 200
    assert dispatcher.calls
    _request_id, method, params, _ctx = dispatcher.calls[-1]
    assert method == "sessions.list"
    assert params == {
        "limit": 200,
        "view": "session-list-v1",
        "cursor": "opaque-cursor",
    }


def test_api_sessions_without_query_params_keeps_default_rpc_params() -> None:
    dispatcher = _FakeDispatcher()

    original = gateway_app.get_dispatcher
    gateway_app.get_dispatcher = lambda: dispatcher
    try:
        app = gateway_app.create_gateway_app(GatewayConfig())
    finally:
        gateway_app.get_dispatcher = original

    with TestClient(app) as client:
        response = client.get("/api/sessions")

    assert response.status_code == 200
    _request_id, method, params, _ctx = dispatcher.calls[-1]
    assert method == "sessions.list"
    assert params is None


def test_api_sessions_maps_empty_cursor_param_to_bad_request() -> None:
    dispatcher = _FakeDispatcher(
        SimpleNamespace(
            ok=False,
            payload=None,
            error=SimpleNamespace(code="INVALID_PARAMS", message="invalid cursor"),
        )
    )

    original = gateway_app.get_dispatcher
    gateway_app.get_dispatcher = lambda: dispatcher
    try:
        app = gateway_app.create_gateway_app(GatewayConfig())
    finally:
        gateway_app.get_dispatcher = original

    with TestClient(app) as client:
        response = client.get("/api/sessions?view=session-list-v1&cursor=")

    assert response.status_code == 400
    assert response.json() == {"error": "invalid cursor"}
    assert dispatcher.calls[-1][2] == {"view": "session-list-v1", "cursor": ""}


def test_api_sessions_maps_out_of_range_cursor_timestamp_to_bad_request() -> None:
    payload = {
        "v": 1,
        "a": 1 << 63,
        "u": 1,
        "k": "agent:main:webchat:cursor-range",
    }
    cursor = _cursor_token(payload)
    app = gateway_app.create_gateway_app(
        GatewayConfig(),
        session_manager=SimpleNamespace(storage=object()),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/sessions",
            params={"view": "session-list-v1", "cursor": cursor},
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": "params.cursor must be a valid sessions.list cursor"
    }


def test_api_sessions_maps_non_utf8_cursor_key_to_bad_request() -> None:
    cursor = _cursor_token({"v": 1, "a": 1, "u": 1, "k": "\ud800"})
    app = gateway_app.create_gateway_app(
        GatewayConfig(),
        session_manager=SimpleNamespace(storage=object()),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/sessions",
            params={"view": "session-list-v1", "cursor": cursor},
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": "params.cursor must be a valid sessions.list cursor"
    }
