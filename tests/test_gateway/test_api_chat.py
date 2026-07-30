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
