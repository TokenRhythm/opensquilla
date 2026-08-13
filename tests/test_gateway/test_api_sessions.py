from __future__ import annotations

from starlette.testclient import TestClient

from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import GatewayConfig


class _FakeDispatchResult:
    ok = True
    payload = {"sessions": [], "count": 0, "ts": 123}
    error = None


class _FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object] | None, object]] = []

    async def dispatch(
        self,
        request_id: str,
        method: str,
        params: dict[str, object] | None,
        ctx: object,
    ) -> _FakeDispatchResult:
        self.calls.append((request_id, method, params, ctx))
        return _FakeDispatchResult()


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
