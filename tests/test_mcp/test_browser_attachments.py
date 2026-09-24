from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.attachment_refs import write_transcript_material
from opensquilla.browser import DesktopBrowserClient
from opensquilla.mcp.desktop_browser import DesktopBrowserMCPClient
from opensquilla.mcp.types import MCPCallContext, current_mcp_call_context
from opensquilla.session.attachment_manifest import (
    MATERIAL_AVAILABLE,
    AttachmentManifest,
    AttachmentOccurrence,
    manifest_context_state,
)
from opensquilla.tools.browser_attachments import (
    MAX_BROWSER_UPLOAD_BYTES,
    BrowserAttachmentError,
    browser_upload_descriptors,
    resolve_browser_upload,
)
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.fixture
def attachment(tmp_path):
    payload = b"First paragraph\n\nSecond paragraph\r\n"
    session_id = "synthetic-session"
    session_key = "agent:main:webchat:synthetic-session"
    digest, path, _ = write_transcript_material(
        media_root=tmp_path, session_id=session_id, payload=payload,
    )
    item = AttachmentOccurrence(
        attachment_id="att_synthetic_fixture", source_message_id="user-message",
        ordinal=0, sha256_ref=digest, name=r"C:\selected\note.txt", mime="text/plain",
        size=len(payload), material_state=MATERIAL_AVAILABLE,
    )
    manifest = AttachmentManifest(session_id, session_key, (item,))
    session = SimpleNamespace(session_id=session_id, epoch=3)
    storage = SimpleNamespace(
        get_context_states=AsyncMock(return_value=[manifest_context_state(manifest)]),
    )
    manager = SimpleNamespace(storage=storage, get_session=AsyncMock(return_value=session))
    context = ToolContext(
        is_owner=True, caller_kind=CallerKind.WEB, session_key=session_key,
        session_id=session_id, session_epoch=3, artifact_session_id=session_id,
        artifact_media_root=str(tmp_path), sandbox_session_manager=manager,
        desktop_browser=DesktopBrowserClient("http://127.0.0.1:43123/v1/browser", "s" * 48),
    )
    return context, item, path, payload


async def test_upload_uses_session_occurrence_and_preserves_exact_bytes(attachment):
    context, item, path, payload = attachment
    descriptors = await browser_upload_descriptors(context)
    assert descriptors == [{
        "fileId": item.attachment_id, "name": "note.txt", "mimeType": "text/plain",
        "size": len(payload),
    }]
    assert str(path) not in str(descriptors)
    packet = await resolve_browser_upload(context, item.attachment_id)
    assert set(packet) == {"fileId", "name", "mimeType", "dataBase64"}
    assert base64.b64decode(packet["dataBase64"]) == payload
    assert context.sandbox_session_manager.get_session.await_count == 3


@pytest.mark.parametrize(
    "file_id", ["../secret", "/etc/passwd", r"C:\private.txt", "att_foreign_file"],
)
async def test_model_paths_and_unknown_occurrences_never_authorize_reads(attachment, file_id):
    context, _item, _path, _payload = attachment
    with pytest.raises(BrowserAttachmentError):
        await resolve_browser_upload(context, file_id)


@pytest.mark.parametrize("change", ["session", "epoch", "artifact_owner"])
async def test_attachment_owner_change_is_rejected(attachment, change):
    context, item, _path, _payload = attachment
    if change == "session":
        context.sandbox_session_manager.get_session.return_value.session_id = "other-session"
    elif change == "epoch":
        context.sandbox_session_manager.get_session.return_value.epoch += 1
    else:
        context.artifact_session_id = "other-session"
    with pytest.raises(BrowserAttachmentError):
        await resolve_browser_upload(context, item.attachment_id)


async def test_changed_material_and_oversized_file_are_rejected_before_encoding(attachment):
    context, item, path, _payload = attachment
    path.write_bytes(b"different data")
    with pytest.raises(BrowserAttachmentError, match="integrity"):
        await resolve_browser_upload(context, item.attachment_id)
    with path.open("wb") as stream:
        stream.truncate(MAX_BROWSER_UPLOAD_BYTES + 1)
    with pytest.raises(BrowserAttachmentError, match="8 MiB"):
        await resolve_browser_upload(context, item.attachment_id)


async def test_symlink_material_is_rejected(attachment, tmp_path):
    context, item, path, payload = attachment
    other = tmp_path / "other-file"
    other.write_bytes(payload)
    path.unlink()
    try:
        path.symlink_to(other)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(BrowserAttachmentError, match="no longer available"):
        await resolve_browser_upload(context, item.attachment_id)


async def test_session_reset_during_material_read_is_rejected(attachment):
    context, item, _path, _payload = attachment
    context.sandbox_session_manager.get_session.side_effect = [
        SimpleNamespace(session_id=context.session_id, epoch=3),
        SimpleNamespace(session_id=context.session_id, epoch=4),
    ]
    with pytest.raises(BrowserAttachmentError, match="session changed"):
        await resolve_browser_upload(context, item.attachment_id)


async def test_upload_boundary_limit_includes_eight_mib(attachment):
    context, item, _path, _payload = attachment
    payload = b"x" * MAX_BROWSER_UPLOAD_BYTES
    digest, _path, _ = write_transcript_material(
        media_root=Path(context.artifact_media_root),
        session_id=context.session_id, payload=payload,
    )
    updated = replace(item, sha256_ref=digest, size=len(payload))
    context.sandbox_session_manager.storage.get_context_states.return_value = [
        manifest_context_state(
            AttachmentManifest(context.session_id, context.session_key, (updated,))
        )
    ]
    packet = await resolve_browser_upload(context, item.attachment_id)
    assert hashlib.sha256(base64.b64decode(packet["dataBase64"])).hexdigest() == digest


async def test_old_desktop_rejects_upload_before_reading_records(attachment, monkeypatch):
    context, item, _path, _payload = attachment
    client = DesktopBrowserMCPClient(context.desktop_browser)
    request = AsyncMock()
    monkeypatch.setattr(client, "_request", request)
    context_token = current_tool_context.set(context)
    call_token = current_mcp_call_context.set(MCPCallContext("call-upload"))
    try:
        result = await client.call_tool("browser_act", {
            "targetRef": "page", "action": "upload", "ref": "input", "fileId": item.attachment_id,
        })
        assert result.is_error and result.structured_content["code"] == "BROWSER_UNSUPPORTED"
        request.assert_not_awaited()
        context.sandbox_session_manager.get_session.assert_not_awaited()
    finally:
        current_tool_context.reset(context_token)
        current_mcp_call_context.reset(call_token)


async def test_upload_capability_is_negotiated_and_cleared_on_disconnect(attachment, monkeypatch):
    context, _item, _path, _payload = attachment
    client = DesktopBrowserMCPClient(context.desktop_browser)
    monkeypatch.setattr(client, "_request", AsyncMock(return_value={"result": {
        "protocolVersion": "2025-06-18", "capabilities": {"experimental": {
            "opensquilla/browser": {"attachmentUploads": True},
        }},
    }}))
    try:
        await client.connect()
        assert client._attachment_uploads
    finally:
        await client.close()
    assert not client._attachment_uploads


async def test_gateway_upload_bytes_are_injected_only_as_trusted_metadata(attachment, monkeypatch):
    context, item, _path, payload = attachment
    client = DesktopBrowserMCPClient(context.desktop_browser)
    client._attachment_uploads = True
    request = AsyncMock(return_value={"result": {"content": [], "isError": False}})
    monkeypatch.setattr(client, "_request", request)
    arguments = {
        "targetRef": "page", "action": "upload", "ref": "input", "fileId": item.attachment_id,
    }
    context_token = current_tool_context.set(context)
    call_token = current_mcp_call_context.set(MCPCallContext("call-upload"))
    try:
        result = await client.call_tool("browser_act", arguments)
        assert not result.is_error
        packet = request.await_args.args[1]
        assert packet["arguments"] == arguments
        assert base64.b64decode(packet["_meta"]["uploadFile"]["dataBase64"]) == payload
        assert str(context.artifact_media_root) not in str(packet)
        request.reset_mock()
        result = await client.call_tool("browser_act", {
            **arguments, "uploadFile": {"fileId": item.attachment_id, "dataBase64": "Zm9yZ2Vk"},
        })
        assert result.is_error and "authority" in result.content
        request.assert_not_awaited()
    finally:
        current_tool_context.reset(context_token)
        current_mcp_call_context.reset(call_token)


async def test_observation_exposes_only_path_free_attachment_descriptors(attachment, monkeypatch):
    context, item, path, payload = attachment
    client = DesktopBrowserMCPClient(context.desktop_browser)
    client._attachment_uploads = True
    monkeypatch.setattr(client, "_request", AsyncMock(return_value={"result": {
        "content": [{"type": "text", "text": "page"}], "structuredContent": {"ok": True},
    }}))
    context_token = current_tool_context.set(context)
    call_token = current_mcp_call_context.set(MCPCallContext("call-inspect"))
    try:
        result = await client.call_tool("browser_inspect", {"targetRef": "page"})
        assert result.structured_content["availableUploads"][0]["fileId"] == item.attachment_id
        assert str(path) not in result.content
        assert base64.b64encode(payload).decode() not in result.content
        assert result.content_blocks[-1]["type"] == "text"
    finally:
        current_tool_context.reset(context_token)
        current_mcp_call_context.reset(call_token)
