from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from opensquilla.content_reader import ContentRange, LegacyContentRef
from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import GatewayConfig
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage


class _Storage:
    def __init__(self) -> None:
        self.body = "hello 你好".encode()
        self.ref = LegacyContentRef("sid", "mid", "active", len(self.body))

    async def get_session(self, key: str):
        return SimpleNamespace(session_key=key, session_id="sid")

    async def get_legacy_content_ref(self, session_id: str, message_id: str, *, source=None):
        if (session_id, message_id) != ("sid", "mid"):
            from opensquilla.content_reader import ContentNotFoundError

            raise ContentNotFoundError("missing")
        return self.ref

    async def read_legacy_content_range(self, ref, *, offset=0, limit=1024 * 1024):
        return ContentRange(ref, offset, limit, self.body[offset : offset + limit])

    async def read_legacy_display_text(self, ref, *, max_bytes=8 * 1024 * 1024):
        return "displayed hello"

    async def read_legacy_display_details(self, ref, *, max_bytes=8 * 1024 * 1024):
        return json.dumps({"id": "mid", "role": "assistant", "reasoning_content": "思考" * 15000})


class _Manager:
    def __init__(self, storage: _Storage) -> None:
        self._storage = storage


def test_content_read_v1_range_and_export_are_bounded(monkeypatch) -> None:
    import opensquilla.gateway.app as gateway_app

    storage = _Storage()
    monkeypatch.setattr(gateway_app, "get_session_storage", lambda _manager: storage)
    app = create_gateway_app(GatewayConfig(), session_manager=_Manager(storage))
    params = {
        "sessionKey": "agent:main:webchat:default",
        "sessionId": "sid",
        "messageId": "mid",
    }
    with TestClient(app, client=("127.0.0.1", 51200)) as client:
        range_response = client.get("/api/content/read", params={**params, "offset": 1, "limit": 3})
        assert range_response.status_code == 200
        assert range_response.content == storage.body[1:4]
        assert range_response.headers["Accept-Ranges"] == "bytes"
        assert range_response.headers["Content-Range"] == "bytes 1-3/12"
        assert range_response.headers["Cache-Control"] == "no-store"

        header_response = client.get(
            "/api/content/read",
            params=params,
            headers={"Range": "bytes=0-4"},
        )
        assert header_response.status_code == 206
        assert header_response.content == storage.body[:5]

        export_response = client.get("/api/content/read", params={**params, "export": "1"})
        assert export_response.status_code == 200
        assert export_response.text == "hello 你好"

        display_response = client.get(
            "/api/content/read",
            params={**params, "view": "display", "export": "1"},
        )
        assert display_response.status_code == 200
        assert display_response.text == "displayed hello"
        assert display_response.headers["X-Content-View"] == "display"
        assert display_response.headers["Cache-Control"] == "no-store"

        details_response = client.get(
            "/api/content/read", params={**params, "view": "details", "export": "1"},
        )
        assert details_response.status_code == 200
        assert details_response.json()["reasoning_content"] == "思考" * 15000
        assert details_response.headers["X-Content-View"] == "details"
        assert details_response.headers["Cache-Control"] == "no-store"
        assert details_response.headers["X-Content-Bytes"] == str(len(details_response.content))

        missing_export = client.get(
            "/api/content/read",
            params={**params, "view": "display"},
        )
        assert missing_export.status_code == 400
        assert missing_export.json()["code"] == "DISPLAY_EXPORT_REQUIRED"


def test_content_read_v1_rejects_missing_identity() -> None:
    app = create_gateway_app(GatewayConfig())
    with TestClient(app) as client:
        response = client.get("/api/content/read", params={"sessionId": "sid"})
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAMS"


@pytest.mark.asyncio
async def test_content_read_display_view_uses_real_storage_projection(tmp_path) -> None:
    guest_key = "osqg_" + ("b" * 43)
    owner_id = hashlib.sha256(guest_key.encode("utf-8")).hexdigest()
    session_key = f"agent:main:webchat:guest:{owner_id}:default"
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    manager = SessionManager(storage, inject_time_prefix=False)
    session = await manager.create(session_key)
    body = '{"text":"' + ("x" * 20_000) + '","display_text":"shown"}'
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_id=session.session_id,
            session_key=session_key,
            message_id="semantic-real",
            role="assistant",
            content=body,
            created_at=1,
        )
    )
    app = create_gateway_app(GatewayConfig(), session_manager=manager)
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/content/read",
                params={
                    "sessionKey": session_key,
                    "sessionId": session.session_id,
                    "messageId": "semantic-real",
                    "view": "display",
                    "export": "1",
                },
                cookies={"opensquilla_guest_session": guest_key},
            )
        assert response.status_code == 200
        assert response.text == "shown"
        assert response.headers["X-Content-View"] == "display"
    finally:
        await storage.close()


def test_content_read_v1_rejects_unknown_source_instead_of_falling_back(monkeypatch) -> None:
    import opensquilla.gateway.app as gateway_app

    storage = _Storage()
    monkeypatch.setattr(gateway_app, "get_session_storage", lambda _manager: storage)
    app = create_gateway_app(GatewayConfig(), session_manager=_Manager(storage))
    params = {
        "sessionKey": "agent:main:webchat:default",
        "sessionId": "sid",
        "messageId": "mid",
        "source": "unexpected",
    }
    with TestClient(app) as client:
        response = client.get("/api/content/read", params=params)
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAMS"


@pytest.mark.parametrize("view", ["raw", "display", "details"])
def test_content_read_v1_enforces_guest_session_ownership(monkeypatch, view) -> None:
    import opensquilla.gateway.app as gateway_app

    storage = _Storage()
    monkeypatch.setattr(gateway_app, "get_session_storage", lambda _manager: storage)
    app = create_gateway_app(GatewayConfig(), session_manager=_Manager(storage))
    guest_key = "osqg_" + ("a" * 43)
    owner_id = hashlib.sha256(guest_key.encode("utf-8")).hexdigest()
    own_key = f"agent:main:webchat:guest:{owner_id}:default"
    foreign = {
        "sessionKey": "agent:main:webchat:default",
        "sessionId": "sid",
        "messageId": "mid",
        "view": view, "export": "1",
    }
    with TestClient(app, client=("10.1.2.3", 51200)) as client:
        response = client.get(
            "/api/content/read",
            params=foreign,
            cookies={"opensquilla_guest_session": guest_key},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "UNAUTHORIZED"

        owned = client.get(
            "/api/content/read",
            params={**foreign, "sessionKey": own_key},
            cookies={"opensquilla_guest_session": guest_key},
        )
        assert owned.status_code == 200


@pytest.mark.parametrize(
    ("scopes", "expected_status"),
    [
        ([], 403),
        (["operator.approvals"], 403),
        (["operator.pairing"], 403),
        (["operator.read"], 200),
        (["operator.write"], 200),
        (["operator.admin"], 200),
    ],
)
@pytest.mark.parametrize("view", ["raw", "display", "details"])
def test_content_read_requires_transcript_read_scope(
    monkeypatch, scopes, expected_status, view,
) -> None:
    import opensquilla.gateway.app as gateway_app

    storage = _Storage()
    monkeypatch.setattr(gateway_app, "get_session_storage", lambda _manager: storage)
    token = "content-read-scope-test-token"
    config = GatewayConfig(auth={"mode": "token", "token": token, "token_scopes": scopes})
    app = create_gateway_app(config, session_manager=_Manager(storage))
    with TestClient(app, client=("127.0.0.1", 51200)) as client:
        response = client.get(
            "/api/content/read",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "sessionKey": "agent:main:webchat:default",
                "sessionId": "sid",
                "messageId": "mid",
                "export": "1",
                "view": view,
            },
        )

    assert response.status_code == expected_status
    if expected_status == 403:
        assert response.json()["code"] == "UNAUTHORIZED"
    elif view == "raw":
        assert response.text == storage.body.decode("utf-8")
    elif view == "details":
        assert response.json()["reasoning_content"] == "思考" * 15000
    else:
        assert response.text == "displayed hello"
