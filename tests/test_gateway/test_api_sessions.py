from __future__ import annotations

from types import SimpleNamespace

from starlette.testclient import TestClient

from opensquilla.gateway.app import create_gateway_app
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


def test_api_sessions_forwards_pagination_query_params() -> None:
    dispatcher = _FakeDispatcher()
    import opensquilla.gateway.app as gateway_app

    original = gateway_app.get_dispatcher
    gateway_app.get_dispatcher = lambda: dispatcher
    try:
        app = create_gateway_app(GatewayConfig())
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
    import opensquilla.gateway.app as gateway_app

    original = gateway_app.get_dispatcher
    gateway_app.get_dispatcher = lambda: dispatcher
    try:
        app = create_gateway_app(GatewayConfig())
    finally:
        gateway_app.get_dispatcher = original

    with TestClient(app) as client:
        response = client.get("/api/sessions")

    assert response.status_code == 200
    _request_id, method, params, _ctx = dispatcher.calls[-1]
    assert method == "sessions.list"
    assert params is None


def test_api_sessions_maps_invalid_cursor_params_to_bad_request() -> None:
    dispatcher = _FakeDispatcher(
        SimpleNamespace(
            ok=False,
            payload=None,
            error=SimpleNamespace(code="INVALID_PARAMS", message="invalid cursor"),
        )
    )
    import opensquilla.gateway.app as gateway_app

    original = gateway_app.get_dispatcher
    gateway_app.get_dispatcher = lambda: dispatcher
    try:
        app = create_gateway_app(GatewayConfig())
    finally:
        gateway_app.get_dispatcher = original

    with TestClient(app) as client:
        response = client.get("/api/sessions?view=session-list-v1&cursor=bad")

    assert response.status_code == 400
    assert response.json() == {"error": "invalid cursor"}
