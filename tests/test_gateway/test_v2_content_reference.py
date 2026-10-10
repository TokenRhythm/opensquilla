"""The v2 history adapter preserves the HTTP content reader's exact identity."""

import hashlib

import pytest
from httpx import ASGITransport, AsyncClient

from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.contracts.generated.v4.sessions_history_page_v2 import HistoryItem
from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc_sessions import _v2_history_item
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage


@pytest.mark.parametrize("view", ["raw", "display", None])
@pytest.mark.parametrize("source", ["active", "compacted"])
def test_v2_reference_preserves_revision_source_and_explicit_view(view, source):
    reference = {
        "version": 1, "sessionId": "sid", "messageId": "mid",
        "revision": f"legacy-v1:{source}:14:1000:2:16735",
        "source": source, "byteLength": 1024 ** 3, "sha256": "a" * 64,
    }
    if view is not None:
        reference["view"] = view
    item = _v2_history_item(
        {"id": "mid", "role": "assistant", "text": "preview", "contentRef": reference},
        session_id="sid", session_epoch=2, order=1,
    )
    HistoryItem.model_validate(item)
    result = item["contents"][0]["ref"]
    assert result["revision"] == reference["revision"]
    assert result["source"] == source
    assert result.get("view") == view
    assert ("view" in result) == (view is not None)
    assert result["sha256"] == reference["sha256"]


def test_v2_does_not_invent_a_ready_revision_when_storage_metadata_is_missing():
    item = _v2_history_item(
        {"id": "mid", "role": "assistant", "text": "preview", "contentRef": {
            "version": 1, "sessionId": "sid", "messageId": "mid", "byteLength": 100,
        }},
        session_id="sid", session_epoch=2, order=1,
    )
    assert item["contents"] == []
    assert item["preview"] == "preview"
    assert item["preview_complete"] is False


@pytest.mark.asyncio
async def test_v2_history_reference_is_accepted_by_the_real_storage_http_reader(tmp_path):
    guest_key = "osqg_" + "v" * 43
    owner_id = hashlib.sha256(guest_key.encode()).hexdigest()
    session_key = f"agent:main:webchat:guest:{owner_id}:default"
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        session = await manager.create(session_key)
        body = "readable history " * 2048
        await storage.append_transcript_entry(TranscriptEntry(
            session_id=session.session_id, session_key=session_key, message_id="mid",
            role="user", content=body, created_at=1,
        ))
        entries, _ = await storage.get_canonical_transcript_page(
            session.session_id, limit=10, content_mode="bounded",
        )
        message = transcript_entries_to_chat_messages(entries, content_mode="bounded")[0]
        item = _v2_history_item(message, session_id=session.session_id, session_epoch=0, order=0)
        HistoryItem.model_validate(item)
        reference = item["contents"][0]["ref"]
        app = create_gateway_app(GatewayConfig(), session_manager=manager)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            client.cookies.set("opensquilla_guest_session", guest_key)
            params = {
                "sessionKey": session_key, "sessionId": reference["session_id"],
                "messageId": item["message_id"], "source": reference["source"],
                "revision": reference["revision"], "offset": 0, "limit": 16,
            }
            response = await client.get("/api/content/read", params=params)
            assert response.status_code == 200
            assert response.content == body.encode()[:16]
            stale = await client.get(
                "/api/content/read", params={**params, "revision": "legacy-v1:mid"}
            )
            assert stale.status_code == 404
    finally:
        await storage.close()
