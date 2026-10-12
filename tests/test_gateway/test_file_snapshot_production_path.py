"""Real file mutation snapshots reach the main authenticated Gateway routes."""

from pathlib import Path
from types import SimpleNamespace

import httpx
from starlette.testclient import TestClient

from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import AttachmentsConfig, AuthConfig, GatewayConfig
from opensquilla.tool_boundary import ToolCall
from opensquilla.tools.builtin.filesystem import write_file
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolContext, ToolSpec


async def test_actual_write_snapshot_is_available_through_main_gateway(tmp_path: Path) -> None:
    session_key = "agent:main:webchat:production-snapshot"
    session_id = "production-snapshot-id"
    media_root = tmp_path / "media"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = GatewayConfig(
        auth=AuthConfig(mode="token", token="fixture-only-token"),
        attachments=AttachmentsConfig(media_root=str(media_root)),
        workspace_dir=str(workspace),
    )

    class Sessions:
        async def get_session(self, key: str):
            return SimpleNamespace(session_id=session_id) if key == session_key else None

    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="write_file", description="Write a file", parameters={
            "path": {"type": "string"}, "content": {"type": "string"},
        }, required=["path", "content"],
    ), write_file)
    handler = build_tool_handler(registry, ToolContext(
        is_owner=True, run_mode="full", session_key=session_key,
        workspace_dir=str(workspace), artifact_media_root=str(media_root),
        artifact_session_id=session_id, sandbox_gateway_config=config,
    ))
    original = "\ufeff" + "historical line\r\n" * 1200
    path = workspace / "history.txt"
    result = await handler(ToolCall(
        tool_use_id="production-write", tool_name="write_file",
        arguments={"path": str(path), "content": original},
    ))
    assert not result.is_error, result.content
    snapshot = result.file_snapshot
    assert snapshot["content"] == ""
    sha = snapshot["sha256_ref"]
    path.write_text("subsequent unrelated content", encoding="utf-8")

    # Use the actual application factory, not a route-only or preview demo app.
    app = create_gateway_app(config, session_manager=Sessions())
    endpoint = f"/api/v1/attachments/{sha}?sessionKey={session_key}"
    headers = {"Authorization": "Bearer fixture-only-token"}
    with TestClient(app) as client:
        preview = client.get(endpoint + "&preview=1", headers=headers)
        download = client.get(endpoint, headers=headers)
        unauthorized = client.get(endpoint + "&preview=1")
        wrong_session = client.get(
            f"/api/v1/attachments/{sha}?sessionKey=agent:main:webchat:other&preview=1",
            headers=headers,
        )
    assert preview.status_code == 200, preview.text
    assert preview.json() == {
        "content": "\ufeff" + "historical line\r\n" * 1000,
        "truncated": True,
        "totalBytes": len(original.encode("utf-8")),
    }
    assert download.status_code == 200
    assert download.content == original.encode("utf-8")
    assert unauthorized.status_code == 401
    assert wrong_session.status_code == 404


async def test_actual_write_snapshot_survives_storage_restart_and_file_change(tmp_path):
    from opensquilla.chat.history import transcript_entries_to_chat_messages
    from opensquilla.engine.runtime import _persisted_tool_result_segment
    from opensquilla.engine.types import ToolResultEvent
    from opensquilla.session.manager import SessionManager
    from opensquilla.session.models import TranscriptEntry
    from opensquilla.session.storage import SessionStorage

    key = "agent:main:webchat:durable-write"
    database = str(tmp_path / "sessions.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = GatewayConfig(
        auth=AuthConfig(mode="token", token="fixture-only-token"),
        attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
        workspace_dir=str(workspace),
    )
    store = await SessionStorage.open(database)
    try:
        manager = SessionManager(store, inject_time_prefix=False)
        session = await manager.create(key)
        registry = ToolRegistry()
        registry.register(ToolSpec(
            name="write_file", description="Write a file", parameters={
                "path": {"type": "string"}, "content": {"type": "string"},
            }, required=["path", "content"],
        ), write_file)
        handler = build_tool_handler(registry, ToolContext(
            is_owner=True, run_mode="full", session_key=key,
            workspace_dir=str(workspace), artifact_media_root=config.attachments.media_root,
            artifact_session_id=session.session_id, sandbox_gateway_config=config,
        ))
        original = "\ufeff" + "written contents 历史\r\n" * 1500
        path = workspace / "history.txt"
        result = await handler(ToolCall(
            tool_use_id="durable-write", tool_name="write_file",
            arguments={"path": str(path), "content": original},
        ))
        assert not result.is_error, result.content
        await store.append_transcript_entry(TranscriptEntry(
            session_id=session.session_id, session_key=key, message_id="durable-message",
            role="assistant", content="Saved", tool_calls=[_persisted_tool_result_segment(
                ToolResultEvent(
                    tool_use_id="durable-write", tool_name="write_file", result=result.content,
                    file_snapshot=result.file_snapshot,
                ),
            )],
        ))
    finally:
        await store.close()
    path.write_text("later unrelated replacement", encoding="utf-8")

    store = await SessionStorage.open(database)
    try:
        manager = SessionManager(store, inject_time_prefix=False)
        entries, has_more = await store.get_canonical_transcript_page(
            session.session_id, limit=10, content_mode="bounded",
        )
        assert has_more is False
        history = transcript_entries_to_chat_messages(entries, content_mode="bounded")
        snapshot = history[0]["tool_calls"][0]["file_snapshot"]
        assert snapshot["content"] == ""
        assert snapshot["bytes"] == len(original.encode())
        sha = snapshot["sha256_ref"]
        app = create_gateway_app(config, session_manager=manager)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            endpoint = f"/api/v1/attachments/{sha}?sessionKey={key}"
            headers = {"Authorization": "Bearer fixture-only-token"}
            preview = await client.get(endpoint + "&preview=1", headers=headers)
            download = await client.get(endpoint, headers=headers)
        assert preview.status_code == 200
        assert preview.json()["truncated"] is True
        assert preview.json()["totalBytes"] == len(original.encode())
        assert download.content == original.encode()
        assert b"later unrelated replacement" not in download.content
    finally:
        await store.close()
