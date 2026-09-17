from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.protocol import ErrorShape, ResFrame


class _ConflictDispatcher:
    def __init__(self, code: str) -> None:
        self._code = code

    async def dispatch(self, request_id, method, params, ctx) -> ResFrame:
        assert method == "chat.send"
        return ResFrame(
            id=request_id,
            ok=False,
            error=ErrorShape(
                code=self._code,
                message="synthetic conflict",
                retryable=True,
                accepted=False,
            ),
        )


@pytest.mark.parametrize(
    "code",
    [
        "COLLECT_RACE",
        "IDEMPOTENCY_CONFLICT",
        "SESSION_CHANGED",
        "SESSION_CONFLICT",
    ],
)
def test_api_chat_maps_turn_conflicts_to_http_409(monkeypatch, code: str) -> None:
    import opensquilla.gateway.app as gateway_app

    monkeypatch.setattr(gateway_app, "get_dispatcher", lambda: _ConflictDispatcher(code))
    app = create_gateway_app(GatewayConfig())

    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={"sessionKey": "agent:main:test:conflict", "message": "hello"},
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": "synthetic conflict",
        "code": code,
        "message": "synthetic conflict",
        "retryable": True,
        "accepted": False,
    }


def test_guest_http_chat_uses_guest_owned_session_namespace() -> None:
    config = GatewayConfig(host="0.0.0.0")
    app = create_gateway_app(config)

    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={
                "sessionKey": "agent:main:webchat:owner-session",
                "message": "hello",
                "intent": "new_chat",
            },
        )

    assert response.status_code == 200
    assert response.json()["sessionKey"].startswith("agent:main:webchat:guest:")


def test_guest_http_chat_identity_is_stable_per_client_and_isolated_between_clients() -> None:
    app = create_gateway_app(GatewayConfig(host="0.0.0.0"))
    payload = {
        "sessionKey": "agent:main:webchat:browser-session",
        "message": "hello",
        "intent": "new_chat",
    }

    with TestClient(app) as first_client:
        first = first_client.post("/api/chat", json=payload)
        second = first_client.post("/api/chat", json=payload)

    with TestClient(app) as second_client:
        isolated = second_client.post("/api/chat", json=payload)

    assert first.status_code == second.status_code == isolated.status_code == 200
    assert first.json()["sessionKey"] == second.json()["sessionKey"]
    assert isolated.json()["sessionKey"] != first.json()["sessionKey"]
    cookie = first.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
