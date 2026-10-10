"""Real SQLite -> history -> existing HTTP route, without inline wire payloads."""

import base64
import hashlib
import io
import json
import random
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from starlette.applications import Starlette

from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.content_reader import ContentNotFoundError, ContentRangeError
from opensquilla.gateway.adapters.content_reader import build_content_reader
from opensquilla.gateway.adapters.session_history_projection import (
    _annotate_transcript_attachment_downloads,
)
from opensquilla.gateway.attachments import register_attachment_routes
from opensquilla.gateway.config import AttachmentsConfig, AuthConfig, GatewayConfig
from opensquilla.gateway.middleware import AuthMiddleware
from opensquilla.gateway.rpc_sessions import _v2_history_item
from opensquilla.session.attachment_manifest import AttachmentManifestStore
from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage

KEY = "agent:main:webchat:attachment-bounded"


def pixels(seed=1):
    buffer = io.BytesIO()
    Image.frombytes("RGB", (320, 320), random.Random(seed).randbytes(320 * 320 * 3)).save(
        buffer, "PNG"
    )
    return buffer.getvalue()


@pytest.fixture
async def storage(tmp_path):
    value = await SessionStorage.open(tmp_path / "history.db")
    await value.upsert_session(SessionNode(session_key=KEY, session_id="sid", agent_id="main"))
    try:
        yield value
    finally:
        await value.close()


async def append(storage, *, text="", payloads=None, indexed=False, archived=False, extras=None):
    payloads = payloads or [pixels()]
    body = json.dumps(
        {
            "text": "provider-only instruction",
            "display_text": text,
            "attachments": [
                {
                    "attachment_id": f"att_synthetic_{i}",
                    "type": "image/png",
                    "name": f"image{i}.png",
                    "data": base64.b64encode(data).decode(),
                }
                for i, data in enumerate(payloads)
            ],
            **(extras or {}),
        }
    )
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_id="sid",
            session_key=KEY,
            message_id="mid",
            role="user",
            content=body,
            created_at=1,
        )
    )
    entry = (await storage.get_transcript("sid"))[0]
    if indexed:
        await AttachmentManifestStore(storage).rebuild(
            session_id="sid", session_key=KEY, entries=[entry]
        )
    if archived:
        await storage._archive_transcript_entries(
            node=SimpleNamespace(session_id="sid"),
            entries=[entry],
            compaction_id="synthetic",
            compaction_index=0,
            source_rows_validated=True,
        )
        await storage.conn.execute("DELETE FROM transcript_entries WHERE id = ?", (entry.id,))
        await storage.conn.commit()
    return body, payloads


async def message(storage):
    entries, _ = await storage.get_canonical_transcript_page(
        "sid", limit=10, content_mode="bounded"
    )
    messages = transcript_entries_to_chat_messages(entries, content_mode="bounded")
    return entries, _annotate_transcript_attachment_downloads(messages, session_key=KEY)[0]


def app_for(storage, tmp_path):
    config = GatewayConfig(auth=AuthConfig(mode="token", token="synthetic-test"),
        attachments=AttachmentsConfig(media_root=str(tmp_path / "media")))
    app = Starlette()
    register_attachment_routes(app, config=config,
        session_manager=SimpleNamespace(storage=storage, get_session=storage.get_session))
    app.add_middleware(AuthMiddleware, config=config)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("text", ["", "A caption with 中文"])
async def test_large_inline_image_survives_bounded_history_and_download(
    storage, tmp_path, indexed, archived, text
):
    body, payloads = await append(storage, text=text, indexed=indexed, archived=archived)
    assert len(body.encode()) > 64 * 1024
    entries, projected = await message(storage)
    assert len(entries[0].content) == 4096
    assert projected["text"] == text
    assert projected["contentPreviewComplete"] is True
    assert "contentRef" not in projected
    attachment = projected["attachments"][0]
    assert attachment["attachment_id"] == "att_synthetic_0"
    assert "data" not in attachment and "_history_source" not in attachment
    assert attachment["sha256_ref"] == hashlib.sha256(payloads[0]).hexdigest()
    item = _v2_history_item(projected, session_id="sid", session_epoch=0, order=0)
    assert item["message"]["attachments"] == [attachment]
    assert len(json.dumps(item).encode()) < 5000
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(storage, tmp_path)),
        base_url="http://test",
    ) as client:
        response = await client.get(
            attachment["download_url"], headers={"Authorization": "Bearer synthetic-test"}
        )
    assert response.status_code == 200
    assert response.content == payloads[0]
    assert response.headers["content-type"].startswith("image/png")
    # No read-induced canonical rewrite, manifest creation, or material copy.
    canonical = await storage.get_canonical_transcript("sid")
    assert canonical[0].content == body
    assert not (tmp_path / "media").exists()
    states = await storage.get_context_states(KEY, state_kind="attachment_manifest_v1")
    assert len(states) == int(indexed)


@pytest.mark.asyncio
async def test_multiple_attachments_use_bounded_chunks_and_keep_metadata(storage, monkeypatch):
    skills = [{"name": "test", "instanceId": "instance", "digest": "d"}]
    workspace = [
        {
            "workspaceId": "workspace",
            "relativePath": "file.txt",
            "name": "file.txt",
            "mime": "text/plain",
        }
    ]
    caption = "caption " * 3000 + "\nC:\\synthetic\\input.txt"
    await append(
        storage,
        text=caption,
        payloads=[pixels(1), pixels(2)],
        extras={
            "selected_skills": skills,
            "page_context": {"url": "https://example.invalid", "title": "Synthetic"},
            "workspace_files": workspace,
            "local_path_references": ["C:\\synthetic\\input.txt"],
        },
    )
    original = storage._read_history_query
    ranges = []

    async def observe(sql, params, *, operation):
        rows = await original(sql, params, operation=operation)
        if operation == "inline_attachment_digests":
            ranges.extend(len(row[1]) for row in rows)
        return rows

    monkeypatch.setattr(storage, "_read_history_query", observe)
    _, projected = await message(storage)
    assert len(projected["attachments"]) == 3
    assert projected["selectedSkills"] == skills
    assert projected["pageContext"]["title"] == "Synthetic"
    assert projected["workspaceFiles"] == workspace
    assert projected["localPathReferences"] == ["C:\\synthetic\\input.txt"]
    assert len(ranges) == 2 and max(ranges) < 200


@pytest.mark.asyncio
async def test_http_inline_attachment_auth_scope_hash_and_revision(storage, tmp_path):
    await append(storage)
    _, projected = await message(storage)
    url = projected["attachments"][0]["download_url"]
    sha = projected["attachments"][0]["sha256_ref"]
    headers = {"Authorization": "Bearer synthetic-test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(storage, tmp_path)), base_url="http://test"
    ) as client:
        assert (await client.get(url)).status_code == 401
        assert (
            await client.get(
                url.replace(
                    "sessionKey=agent%3Amain%3Awebchat%3Aattachment-bounded",
                    "sessionKey=agent%3Amain%3Awebchat%3Aother",
                ),
                headers=headers,
            )
        ).status_code == 404
        assert (await client.get(url.replace(sha, "0" * 64), headers=headers)).status_code == 409
        assert (
            await client.get(
                url.replace("attachmentIndex=0", "attachmentIndex=16"), headers=headers
            )
        ).status_code == 404
        await storage.conn.execute(
            "UPDATE transcript_entries SET content = "
            "replace(content, 'provider-only', 'provider-new!') WHERE message_id = 'mid'"
        )
        assert (await client.get(url, headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_inline_read_rejects_non_user_and_changed_ref(storage):
    await append(storage)
    ref = await storage.get_legacy_content_ref("sid", "mid")
    with pytest.raises(ContentNotFoundError):
        await storage.read_inline_attachment(replace(ref, revision=ref.revision + "changed"), 0)
    with pytest.raises(ContentRangeError):
        await storage.read_inline_attachment(ref, 0, max_bytes=1)
    await storage.conn.execute(
        "UPDATE transcript_entries SET role = 'assistant' WHERE message_id = 'mid'"
    )
    with pytest.raises(ContentNotFoundError):
        await storage.read_inline_attachment(ref, 0)


@pytest.mark.asyncio
async def test_invalid_inline_material_is_visible_local_failure(storage):
    await append(
        storage,
        extras={"attachments": [{"name": "bad.png", "type": "image/png", "data": "!not base64!"}]},
    )
    _, projected = await message(storage)
    assert projected["text"] == ""
    assert projected["attachments"][0]["missing_reason"] == "attachment preview unavailable"
    assert "download_url" not in projected["attachments"][0]


@pytest.mark.asyncio
async def test_actual_size_guard_ignores_wrong_metadata_without_full_body_read(
    storage, monkeypatch
):
    import opensquilla.session.attachment_history as policy
    import opensquilla.session.storage as module

    monkeypatch.setattr(policy, "MAX_INLINE_ENVELOPE_BYTES", 1024)
    monkeypatch.setattr(module, "_BOUNDED_USER_ENVELOPE_SQL", policy.user_display_envelope_sql())
    await append(storage)
    await storage.conn.execute("UPDATE transcript_entries SET content_byte_length = 0")
    entries, _ = await storage.get_canonical_transcript_page(
        "sid", limit=10, content_mode="bounded"
    )
    assert len(entries[0].content) <= 64 * 1024
    assert entries[0].user_display_envelope.get("error")
    projected = transcript_entries_to_chat_messages(entries, content_mode="bounded")[0]
    assert projected["text"].startswith("[Attachment preview unavailable:")


@pytest.mark.asyncio
async def test_small_and_large_text_only_envelopes_keep_display_semantics(storage):
    await append(storage, text="caption " * 4000)
    _, projected = await message(storage)
    assert projected["text"] == ("caption " * 4000)[:16 * 1024]
    assert len(projected["attachments"]) == 1
    assert projected["contentRef"]["view"] == "display"
    ref = await storage.get_legacy_content_ref("sid", "mid")
    assert await build_content_reader(storage).read_display_text(ref) == "caption " * 4000


@pytest.mark.asyncio
async def test_unindexed_byte_length_does_not_hide_readable_image(storage):
    await append(storage)
    await storage.conn.execute("UPDATE transcript_entries SET content_byte_length = NULL")
    _, projected = await message(storage)
    attachment = projected["attachments"][0]
    assert "missing_reason" not in attachment
    assert attachment["download_url"]
    assert projected.get("contentMetadataPending")
    assert projected["contentPreviewComplete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("archived", [False, True])
async def test_unindexed_inline_caption_http_preserves_revision_and_image(storage, archived):
    from opensquilla.gateway.app import create_gateway_app

    caption = "图片正文🙂 café\n" * 3000
    _, payloads = await append(storage, text=caption, archived=archived)
    table = "compacted_transcript_entries" if archived else "transcript_entries"
    await storage.conn.execute(f"UPDATE {table} SET content_byte_length = NULL")
    await storage.conn.commit()
    _, projected = await message(storage)
    reference = projected["contentRef"]
    attachment_url = projected["attachments"][0]["download_url"]
    app = create_gateway_app(
        GatewayConfig(auth=AuthConfig(mode="token", token="synthetic-test")),
        session_manager=SimpleNamespace(storage=storage, get_session=storage.get_session),
    )
    params = {"sessionKey": KEY, "sessionId": "sid", "messageId": "mid",
        "source": "compacted" if archived else "active", "revision": reference["revision"],
        "view": "display", "export": "1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1", headers={"Authorization": "Bearer synthetic-test"}) as client:
        response = await client.get("/api/content/read", params=params)
        assert response.status_code == 200
        assert response.text == caption
        assert reference["revision"].endswith(":pending")
        assert "byteLength" not in reference
        assert (await client.get("/api/content/read", params={**params,
            "revision": reference["revision"].removesuffix("pending") + "1"})).status_code == 404
        image = await client.get(attachment_url)
        assert image.status_code == 200 and image.content == payloads[0]
        await storage.backfill_transcript_content_lengths(batch_size=4)
        assert (await client.get("/api/content/read", params=params)).text == caption
        _, indexed = await message(storage)
        assert not indexed["contentRef"]["revision"].endswith(":pending")
        assert (await client.get("/api/content/read", params={**params,
            "revision": indexed["contentRef"]["revision"]})).text == caption
        await storage.conn.execute(
            f"UPDATE {table} SET content = replace(content, 'provider-only', 'provider-next')"
        )
        await storage.conn.commit()
        assert (await client.get("/api/content/read", params=params)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("archived", [False, True])
async def test_pending_caption_rejects_same_length_update_between_lookup_and_read(
    storage, monkeypatch, archived,
):
    await append(storage, text="caption🙂" * 4000, archived=archived)
    table = "compacted_transcript_entries" if archived else "transcript_entries"
    source = "compacted" if archived else "active"
    await storage.conn.execute(f"UPDATE {table} SET content_byte_length = NULL")
    await storage.conn.commit()
    reference = await storage.get_legacy_content_ref(
        "sid", "mid", source=source, allow_pending=True
    )
    read_unindexed = storage._read_unindexed_display_entry

    async def update_before_caption(ref, *, max_bytes):
        await storage.conn.execute(
            f"UPDATE {table} SET content = replace(content, 'provider-only', 'provider-next')"
        )
        await storage.conn.commit()
        return await read_unindexed(ref, max_bytes=max_bytes)

    monkeypatch.setattr(storage, "_read_unindexed_display_entry", update_before_caption)
    with pytest.raises(ContentNotFoundError):
        await build_content_reader(storage).read_display_text(reference)


@pytest.mark.asyncio
@pytest.mark.parametrize("unindexed", [False, True])
@pytest.mark.parametrize("archived", [False, True])
async def test_caption_hydrates_when_image_envelope_exceeds_display_export_limit(
    storage, unindexed, archived,
):
    from opensquilla.gateway.app import create_gateway_app
    payload = pixels()
    payload += bytes(5 * 1024 * 1024 - len(payload))
    caption = "visible caption 中文\n" * 1500
    body, _ = await append(storage, text=caption, payloads=[payload, payload], archived=archived)
    assert len(body.encode()) > 8 * 1024 * 1024
    source = "compacted" if archived else "active"
    if unindexed:
        table = "compacted_transcript_entries" if archived else "transcript_entries"
        await storage.conn.execute(f"UPDATE {table} SET content_byte_length = NULL")
        await storage.conn.commit()
    _, projected = await message(storage)
    assert projected["contentRef"]["view"] == "display"
    ref = await storage.get_legacy_content_ref("sid", "mid", source=source, allow_pending=True)
    assert await build_content_reader(storage).read_display_text(ref) == caption
    with pytest.raises(ContentNotFoundError):
        await build_content_reader(storage).read_display_text(
            replace(ref, revision=ref.revision + "stale"),
        )
    app = create_gateway_app(
        GatewayConfig(),
        session_manager=SimpleNamespace(storage=storage, get_session=storage.get_session),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/content/read", params={
            "sessionKey": KEY, "sessionId": "sid", "messageId": "mid", "source": source,
            "revision": ref.revision, "view": "display", "export": "1",
        })
    assert response.status_code == 200
    assert response.text == caption
    assert int(response.headers["X-Content-Bytes"]) == len(caption.encode())


@pytest.mark.asyncio
async def test_guest_cannot_read_another_sessions_inline_attachment(storage, tmp_path):
    from starlette.middleware.base import BaseHTTPMiddleware

    from opensquilla.gateway.auth import Principal

    await append(storage)
    _, projected = await message(storage)
    app = app_for(storage, tmp_path)
    # Exercise the same guest proof as content.read.v1. This middleware
    # deliberately stands in for principal resolution, not token validation.
    class GuestPrincipalMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.principal = Principal(
                "operator", frozenset(), False, False, guest_owner_id="a" * 64
            )
            return await call_next(request)
    app.user_middleware = []
    app.add_middleware(GuestPrincipalMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get(projected["attachments"][0]["download_url"])).status_code == 403
