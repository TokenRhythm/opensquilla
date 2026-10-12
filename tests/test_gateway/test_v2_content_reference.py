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


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["active", "compacted"])
@pytest.mark.parametrize("long_body", [False, True])
async def test_unindexed_details_have_a_readable_identity_without_a_length(
    tmp_path, source, long_body,
):
    guest_key = "osqg_" + "u" * 43
    owner_id = hashlib.sha256(guest_key.encode()).hexdigest()
    session_key = f"agent:main:webchat:guest:{owner_id}:default"
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        session = await manager.create(session_key)
        reasoning = "reasoning " * 4000
        body = "正文🙂\n" * 20000 if long_body else "Complete answer"
        await storage.append_transcript_entry(TranscriptEntry(
            session_id=session.session_id, session_key=session_key, message_id="mid",
            role="assistant", content=body, reasoning_content=reasoning,
            tool_calls=[{"type": "tool_result", "tool_use_id": "t", "content": "result " * 5000}],
            created_at=1,
        ))
        table = "transcript_entries"
        if source == "compacted":
            await storage.conn.execute("""
                INSERT INTO compacted_transcript_entries
                  (session_id, session_key, message_id, role, content, tool_calls,
                   reasoning_content, created_at, archived_at, original_entry_id,
                   content_byte_length, content_revision)
                SELECT session_id, session_key, message_id, role, content, tool_calls,
                       reasoning_content, created_at, 2, id, content_byte_length, content_revision
                FROM transcript_entries
            """)
            await storage.conn.execute("DELETE FROM transcript_entries")
            table = "compacted_transcript_entries"
        await storage.conn.execute(f"UPDATE {table} SET content_byte_length = NULL")
        await storage.conn.commit()
        entries, _ = await storage.get_canonical_transcript_page(
            session.session_id, limit=10, content_mode="bounded",
        )
        message = transcript_entries_to_chat_messages(entries, content_mode="bounded")[0]
        item = _v2_history_item(message, session_id=session.session_id, session_epoch=0, order=0)
        HistoryItem.model_validate(item)
        assert item["preview_complete"] is (not long_body)
        assert item["content_availability"] == "preparing"
        assert item["contents"] == []
        reference = item["message"]["contentRef"]
        assert reference["view"] == "display"
        assert reference["revision"].endswith(":pending")
        assert "byteLength" not in reference
        app = create_gateway_app(GatewayConfig(), session_manager=manager)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        ) as client:
            client.cookies.set("opensquilla_guest_session", guest_key)
            params = {
                "sessionKey": session_key, "sessionId": session.session_id,
                "messageId": "mid", "source": source, "revision": reference["revision"],
                "view": "details", "export": "1",
            }
            response = await client.get("/api/content/read", params=params)
            assert response.status_code == 200
            assert response.json()["reasoning_content"] == reasoning
            assert response.json()["tool_calls"][0]["content"] == "result " * 5000
            display = await client.get("/api/content/read", params={**params, "view": "display"})
            assert display.status_code == 200
            assert display.text == body
            assert display.headers["x-content-revision"] == reference["revision"]
            raw = await client.get("/api/content/read", params={**params, "view": "raw"})
            assert raw.status_code == 503
            await storage.backfill_transcript_content_lengths(batch_size=4)
            assert (await client.get("/api/content/read", params=params)).status_code == 200
            after = await client.get("/api/content/read", params={**params, "view": "display"})
            assert after.text == body
            await storage.conn.execute(
                f"UPDATE {table} SET reasoning_content = ? WHERE message_id = 'mid'",
                (reasoning.replace("reasoning", "replaced!"),),
            )
            await storage.conn.commit()
            assert (await client.get("/api/content/read", params=params)).status_code == 404
    finally:
        await storage.close()


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
