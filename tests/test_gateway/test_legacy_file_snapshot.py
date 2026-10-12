"""Historical write details are recovered by identity only when opened."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from opensquilla.attachment_refs import transcript_material_path
from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.chat.legacy_file_snapshot import MAX_LEGACY_REPLAY_BYTES
from opensquilla.gateway.attachments import register_attachment_routes
from opensquilla.gateway.config import AttachmentsConfig, AuthConfig, GatewayConfig
from opensquilla.gateway.legacy_file_snapshot import LegacyFileSnapshotRecovery
from opensquilla.gateway.middleware import AuthMiddleware
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage

KEY = "agent:main:webchat:legacy-write"
MESSAGE = "legacy-write-message"
TOOL = "legacy-write-tool"
PATH = "games/snake.html"
HEADERS = {"Authorization": "Bearer test-only-secret"}


def _records(body: str):
    sha = hashlib.sha256(body.encode()).hexdigest()
    marker = (
        "[historical_tool_argument_omitted]\n"
        f"tool: write_file\ntool_use_id: {TOOL}\nfield: content\n"
        f"original_chars: {len(body)}\nsha256: {sha}\npath: {PATH}\n"
        f"head:\n{body[:512]}\ntail:\n{body[-512:]}"
    )
    segments = [
        {"type": "tool_use", "name": "write_file", "tool_use_id": TOOL,
         "input": {"path": PATH, "content": marker}},
        {"type": "tool_result", "name": "write_file", "tool_use_id": TOOL,
         "result": "Written historical file", "is_error": False},
    ]
    replay = {"version": 1, "messages": [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": TOOL, "name": "write_file",
             "input": {"path": PATH, "content": body}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": TOOL,
             "content": "Written historical file", "is_error": False},
        ]},
    ]}
    return sha, segments, replay


def _url(sha: str, *, key=KEY, message=MESSAGE, tool=TOOL, preview=True):
    return (
        f"/api/v1/attachments/{sha}?sessionKey={key}&messageId={message}&toolUseId={tool}"
        + ("&preview=1" if preview else "")
    )


def test_history_advertises_only_small_reference_without_accessing_replay():
    body = "<p>历史内容</p>\n" * 2000
    sha, segments, _replay = _records(body)

    class Entry(SimpleNamespace):
        @property
        def assistant_replay(self):
            raise AssertionError("listing history must not access provider replay")

    entry = Entry(role="assistant", message_id=MESSAGE, content="Saved", tool_calls=segments)
    messages = transcript_entries_to_chat_messages([entry])
    snapshot = messages[0]["tool_calls"][1]["file_snapshot"]
    assert snapshot == {
        "kind": "write", "path": PATH, "content": "",
        "legacy_ref": {"message_id": MESSAGE, "tool_use_id": TOOL,
                       "sha256": sha, "original_chars": len(body)},
    }
    assert len(json.dumps(messages).encode()) < 10_000
    assert "file_snapshot" not in segments[1]
    assert "bytes" not in snapshot  # Character counts are not byte lengths.


@pytest.mark.parametrize("mutation", ["failed", "cancelled", "missing", "duplicate", "path", "id"])
def test_history_does_not_advertise_unverifiable_writes(mutation):
    _sha, segments, _replay = _records("x" * 5000)
    if mutation == "failed":
        segments[1]["is_error"] = True
    elif mutation == "cancelled":
        segments[1]["execution_status"] = {"status": "cancelled"}
    elif mutation == "missing":
        segments.pop()
    elif mutation == "duplicate":
        segments.append(copy.deepcopy(segments[1]))
    elif mutation == "path":
        segments[0]["input"]["path"] = "other.html"
    else:
        segments[0]["input"]["content"] = segments[0]["input"]["content"].replace(
            f"tool_use_id: {TOOL}", "tool_use_id: other-tool",
        )
    entry = SimpleNamespace(
        role="assistant", message_id=MESSAGE, content="Saved", tool_calls=segments,
    )
    messages = transcript_entries_to_chat_messages([entry])
    assert all("file_snapshot" not in segment for segment in messages[0]["tool_calls"])


@pytest.fixture
async def stored_app(tmp_path):
    store = await SessionStorage.open(str(tmp_path / "sessions.db"))
    manager = SessionManager(store, inject_time_prefix=False)
    session = await manager.create(KEY)
    config = GatewayConfig(
        auth=AuthConfig(mode="token", token="test-only-secret"),
        attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
    )
    app = Starlette()
    register_attachment_routes(app, config=config, session_manager=manager)
    app.add_middleware(AuthMiddleware, config=config)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            yield store, session, config, client
    finally:
        await store.close()


async def _save(store, session, body, *, mutation=None):
    sha, segments, replay = _records(body)
    if mutation == "hash":
        replay["messages"][0]["content"][0]["input"]["content"] = "y" * len(body)
    elif mutation == "path":
        replay["messages"][0]["content"][0]["input"]["path"] = "other.html"
    elif mutation == "failed":
        replay["messages"][1]["content"][0]["is_error"] = True
    elif mutation == "cancelled":
        replay["messages"][1]["content"][0]["execution_status"] = {"status": "cancelled"}
    elif mutation == "oversized":
        replay["messages"].append({"role": "assistant", "content": "z" * MAX_LEGACY_REPLAY_BYTES})
    elif mutation == "missing":
        replay = None
    await store.append_transcript_entry(TranscriptEntry(
        session_id=session.session_id, session_key=KEY, message_id=MESSAGE,
        role="assistant", content="Saved", tool_calls=segments, assistant_replay=replay,
    ))
    return sha


async def test_expansion_recovers_exact_content_and_reuses_material(stored_app, monkeypatch):
    store, session, config, client = stored_app
    body = "\ufeff" + "历史内容\r\n" * 1500
    sha = await _save(store, session, body)
    reader = store.get_canonical_transcript_replay
    reads = []

    async def read_one(session_id, message_id, *, max_bytes):
        reads.append((session_id, message_id, max_bytes))
        return await reader(session_id, message_id, max_bytes=max_bytes)

    monkeypatch.setattr(store, "get_canonical_transcript_replay", read_one)
    material = transcript_material_path(
        config.attachments.media_root, session.session_id, sha,
    )
    assert not material.exists()
    response = await client.get(_url(sha), headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        "content": "\ufeff" + "历史内容\r\n" * 1000,
        "truncated": True, "totalBytes": len(body.encode()),
    }
    download = await client.get(_url(sha, preview=False), headers=HEADERS)
    assert download.content == body.encode()
    assert material.read_bytes() == body.encode()
    assert reads == [(session.session_id, MESSAGE, MAX_LEGACY_REPLAY_BYTES)]
    # A cached SHA must not bypass the legacy call/session locator check.
    for url in (
        _url(sha, message="other-message"), _url(sha, tool="other-tool"),
        _url(sha, key="agent:main:webchat:other"),
    ):
        assert (await client.get(url, headers=HEADERS)).status_code == 404
    assert (await client.get(_url(sha))).status_code == 401


@pytest.mark.parametrize("mutation,status", [
    ("hash", 409), ("path", 404), ("failed", 404), ("cancelled", 404),
    ("oversized", 404), ("missing", 404),
])
async def test_unverifiable_replay_never_creates_material(stored_app, mutation, status):
    store, session, config, client = stored_app
    sha = await _save(store, session, "x" * 5000, mutation=mutation)
    response = await client.get(_url(sha), headers=HEADERS)
    assert response.status_code == status
    assert not transcript_material_path(
        config.attachments.media_root, session.session_id, sha,
    ).exists()


@pytest.mark.parametrize("replace_session", [False, True], ids=["delete", "delete-and-recreate"])
async def test_recovery_cannot_recreate_deleted_session_material(
    stored_app, monkeypatch, replace_session,
):
    from opensquilla.gateway import legacy_file_snapshot as recovery_module
    from opensquilla.gateway.boot import build_session_material_cleanup
    from opensquilla.session.material_cleanup import (
        reset_session_material_cleanup,
        set_session_material_cleanup,
    )

    store, session, config, client = stored_app
    sha = await _save(store, session, "private historical file\n" * 1000)
    material = transcript_material_path(config.attachments.media_root, session.session_id, sha)
    workspace = Path(config.attachments.media_root).parent / "workspace"
    workspace.mkdir()
    config.workspace_dir = str(workspace)
    entered = threading.Event()
    release = threading.Event()
    persist = recovery_module._persist_verified_write

    def delayed_persist(*args, **kwargs):
        entered.set()
        assert release.wait(5), "recovery worker was not released"
        return persist(*args, **kwargs)

    monkeypatch.setattr(recovery_module, "_persist_verified_write", delayed_persist)
    set_session_material_cleanup(build_session_material_cleanup(config))
    request = asyncio.create_task(client.get(_url(sha), headers=HEADERS))
    replacement_material = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
        await store.delete_session(KEY)
        assert await store.get_session(KEY) is None
        assert not material.parent.exists()
        if replace_session:
            replacement = await SessionManager(store, inject_time_prefix=False).create(KEY)
            assert replacement.session_id != session.session_id
            replacement_material = transcript_material_path(
                config.attachments.media_root, replacement.session_id, sha,
            )
            replacement_material.parent.mkdir(parents=True)
            replacement_material.write_bytes(b"new session material")
        release.set()
        response = await asyncio.wait_for(request, 5)
        assert response.status_code == 404
        assert not material.exists()
        assert not material.parent.exists()
        if replacement_material is not None:
            assert replacement_material.read_bytes() == b"new session material"
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        reset_session_material_cleanup()


async def test_recovery_is_single_flight_and_limited_to_two_workers(tmp_path, monkeypatch):
    body = "x" * 5000
    sha, segments, replay = _records(body)
    release = asyncio.Event()
    two_active = asyncio.Event()
    active = 0
    peak = 0
    reads = []

    class Storage:
        async def get_session(self, key):
            return SimpleNamespace(session_id=key, epoch=0)

        async def get_legacy_content_ref(self, session, message, *, allow_pending):
            assert allow_pending is True
            return SimpleNamespace(session_id=session, message_id=message)

        async def read_legacy_detail_entry(self, ref, *, max_bytes):
            assert max_bytes == MAX_LEGACY_REPLAY_BYTES
            return SimpleNamespace(role="assistant", tool_calls=segments)

        async def get_canonical_transcript_replay(self, session, message, *, max_bytes):
            nonlocal active, peak
            reads.append(session)
            assert max_bytes == MAX_LEGACY_REPLAY_BYTES
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_active.set()
            await release.wait()
            active -= 1
            return replay

    service = LegacyFileSnapshotRecovery(
        GatewayConfig(attachments=AttachmentsConfig(media_root=str(tmp_path))),
        SimpleNamespace(storage=Storage()),
    )
    tasks = [asyncio.create_task(service.recover(
        session_key=key, message_id=MESSAGE, tool_use_id=TOOL, sha256=sha,
    )) for key in ("session-1", "session-1", "session-2", "session-3")]
    await asyncio.wait_for(two_active.wait(), 2)
    assert len(reads) == 2
    tasks[0].cancel()
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1:] == [None, None, None]
    assert peak == 2
    assert sorted(reads) == ["session-1", "session-2", "session-3"]
    assert not service._pending


async def test_recovery_admission_is_bounded_and_duplicate_joins_remain_available(
    tmp_path, monkeypatch,
):
    from opensquilla.gateway.legacy_file_snapshot import LegacyFileSnapshotBusyError

    release = asyncio.Event()
    started = asyncio.Event()
    count = 0

    async def blocked(*_args):
        nonlocal count
        count += 1
        if count == 16:
            started.set()
        await release.wait()

    service = LegacyFileSnapshotRecovery(
        GatewayConfig(attachments=AttachmentsConfig(media_root=str(tmp_path))),
        SimpleNamespace(storage=object()),
    )
    monkeypatch.setattr(service, "_recover_one", blocked)
    sha = "a" * 64
    tasks = [asyncio.create_task(service.recover(
        session_key=f"session-{index}", message_id=MESSAGE, tool_use_id=TOOL, sha256=sha,
    )) for index in range(16)]
    await asyncio.wait_for(started.wait(), 2)
    with pytest.raises(LegacyFileSnapshotBusyError):
        await service.recover(
            session_key="overflow", message_id=MESSAGE, tool_use_id=TOOL, sha256=sha,
        )
    duplicate = asyncio.create_task(service.recover(
        session_key="session-0", message_id=MESSAGE, tool_use_id=TOOL, sha256=sha,
    ))
    release.set()
    await asyncio.gather(*tasks, duplicate)
    assert count == 16
    assert not service._pending
