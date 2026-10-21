"""File details follow transcript lifetime independently of model/cache content."""

from __future__ import annotations

import hashlib
import json
import os
import time

import pytest

from opensquilla.attachment_refs import transcript_material_path
from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.engine.history import reconstruct_messages_from_entry
from opensquilla.engine.runtime import _persisted_tool_result_segment
from opensquilla.engine.tool_result_store import ToolResultStore
from opensquilla.engine.turn_runner.stream_consumer_stage import (
    _StreamState,
    _ToolResultHandler,
    _ToolUseStartHandler,
)
from opensquilla.engine.types import ToolResultEvent, ToolUseStartEvent, public_agent_event_payload
from opensquilla.file_snapshot import (
    MAX_EDIT_SNAPSHOT_BYTES,
    build_file_edit_snapshot,
    retain_file_snapshot,
)
from opensquilla.gateway.channel_dispatch import _tool_result_payload
from opensquilla.gateway.snapshot_transfer import SnapshotTransferError, freeze_json
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionSummary, TranscriptEntry
from opensquilla.session.storage import SessionStorage, _transcript_preimage
from opensquilla.tools.types import ToolContext


def _segments(content: str, *, call_id: str = "write-1") -> list[dict]:
    state = _StreamState()
    _ToolUseStartHandler().handle(
        ToolUseStartEvent(tool_use_id=call_id, tool_name="write_file"), state,
    )
    _ToolResultHandler().handle(
        ToolResultEvent(
            tool_use_id=call_id,
            tool_name="write_file",
            arguments={"path": "page.html", "content": content},
            result="Written page.html",
            file_snapshot={"kind": "write", "path": "page.html", "content": content},
        ),
        state,
    )
    return state.turn_segments


@pytest.mark.parametrize("content", ["", "<h1>你好</h1>\r\n" * 2000], ids=["empty", "large"])
def test_file_snapshot_is_public_display_data_not_provider_replay(content):
    segments = _segments(content)
    snapshot = segments[1]["file_snapshot"]
    assert snapshot["content"] == content
    if len(content) > 4096:
        assert segments[0]["input"]["content"].startswith("[historical_tool_argument_omitted]")

    entry = TranscriptEntry(
        session_id="file-history", session_key="agent:main:webchat:file-history",
        role="assistant", content="Saved.", tool_calls=segments,
    )
    ui = transcript_entries_to_chat_messages([entry])
    assert ui[0]["tool_calls"][1]["file_snapshot"] == snapshot
    replay = [message.model_dump(mode="json") for message in reconstruct_messages_from_entry(
        entry.role, entry.content, entry.tool_calls,
    )]
    serialized = json.dumps(replay, ensure_ascii=False)
    assert "file_snapshot" not in serialized
    if len(content) > 4096:
        assert content not in serialized

    event = ToolResultEvent(tool_name="write_file", result="Written", file_snapshot=snapshot)
    assert public_agent_event_payload(event)["file_snapshot"] == snapshot
    assert _tool_result_payload(event)["file_snapshot"] == snapshot


def test_failed_result_does_not_persist_or_forward_success_snapshot():
    event = ToolResultEvent(
        tool_name="edit_file", is_error=True, result="Write failed",
        file_snapshot={"kind": "edit", "path": "page.html", "content": "-old\n+new\n"},
    )
    assert "file_snapshot" not in _persisted_tool_result_segment(event)
    assert "file_snapshot" not in _tool_result_payload(event)
    assert "file_snapshot" not in public_agent_event_payload(event)


@pytest.mark.asyncio
async def test_large_applied_diff_keeps_live_and_history_transport_bounded(tmp_path):
    # Use a small transport budget to prove admission independently of the
    # production diff ceiling: complete source is durable, envelopes are small.
    transport_budget = 16_384
    original = "a" * (32 * 1024) + "x\n"
    full = build_file_edit_snapshot("large.json", original, original[:-2] + "y\n")
    assert transport_budget < len(full["content"].encode("utf-8")) < MAX_EDIT_SNAPSHOT_BYTES
    with pytest.raises(SnapshotTransferError, match="SNAPSHOT_TOO_LARGE"):
        freeze_json(full, max_bytes=transport_budget)
    media_root = tmp_path / "media"
    snapshot = await retain_file_snapshot(full, context=ToolContext(
        artifact_media_root=str(media_root), artifact_session_id="large-history",
    ))
    assert snapshot["bytes"] == len(full["content"].encode("utf-8"))
    material = transcript_material_path(media_root, "large-history", snapshot["sha256_ref"])
    assert material.read_bytes().decode("utf-8") == full["content"]
    event = ToolResultEvent(
        tool_use_id="large-edit", tool_name="edit_file", result="Edited large.json",
        arguments={"path": "large.json", "old_text": "x", "new_text": "y"},
        file_snapshot=snapshot,
    )
    entry = TranscriptEntry(
        session_id="large-history", session_key="agent:main:webchat:large-history",
        role="assistant", content="Edited.", tool_calls=[_persisted_tool_result_segment(event)],
    )
    for payload in (
        public_agent_event_payload(event), _tool_result_payload(event),
        transcript_entries_to_chat_messages([entry]),
    ):
        freeze_json(payload, max_bytes=transport_budget)
        assert len(json.dumps(payload).encode("utf-8")) < transport_budget
    replay = [message.model_dump(mode="json") for message in reconstruct_messages_from_entry(
        entry.role, entry.content, entry.tool_calls,
    )]
    assert "file_snapshot" not in json.dumps(replay)
    assert snapshot["sha256_ref"] not in json.dumps(replay)


@pytest.mark.asyncio
async def test_history_survives_restart_cache_cleanup_compaction_fork_and_parent_delete(tmp_path):
    db = str(tmp_path / "sessions.db")
    store = await SessionStorage.open(db)
    manager = SessionManager(store, inject_time_prefix=False)
    parent = await manager.create("agent:main:webchat:file-history")
    original = "<html>\n" + "内容\r\n" * 6000 + "</html>"
    updated = "<html>second version</html>"
    old_time = time.time() - 10 * 24 * 60 * 60
    expected = [original, updated]
    try:
        for index, body in enumerate(expected):
            await store.append_transcript_entry(TranscriptEntry(
                session_id=parent.session_id, session_key=parent.session_key,
                role="assistant", content="Saved.", message_id=f"answer-{index}",
                tool_calls=_segments(body, call_id=f"write-{index}"),
                created_at=int(old_time * 1000) + index,
            ))
    finally:
        await store.close()

    current = tmp_path / "page.html"
    current.write_text("unrelated later edit", encoding="utf-8")
    current.unlink()

    cache = ToolResultStore(tmp_path / "tool-results")
    common = dict(session_id=parent.session_id, session_key=parent.session_key, agent_id="main")
    expired = cache.write("expired log", tool_use_id="log-1", tool_name="exec", **common)
    cache_file = cache._record_dir(expired.handle, session_id=parent.session_id) / "content.txt"
    os.utime(cache_file, (old_time, old_time))
    victim = cache.write("old" * 20, tool_use_id="log-2", tool_name="exec", **common)
    cache.write("new" * 20, tool_use_id="log-3", tool_name="exec", disk_budget_bytes=60, **common)
    for record in (expired, victim):
        with pytest.raises(FileNotFoundError):
            cache.read(record.handle, session_id=parent.session_id)

    store = await SessionStorage.open(db)
    try:
        entries = await store.get_transcript(parent.session_id)
        assert [entry.tool_calls[1]["file_snapshot"]["content"] for entry in entries] == expected
        restored = entries[0].tool_calls[1]["file_snapshot"]["content"]
        assert hashlib.sha256(restored.encode()).digest() == (
            hashlib.sha256(original.encode()).digest()
        )
        parent.compaction_count = 1
        assert await store.rewrite_compacted_session(
            node=parent,
            summary=SessionSummary(
                session_id=parent.session_id, session_key=parent.session_key,
                summary_text="Saved the page.", removed_count=1, kept_count=1,
            ),
            entries=entries[1:], archived_entries=entries[:1],
            expected_source_entries=entries,
            expected_source_preimage=_transcript_preimage(entries),
            expected_session_id=parent.session_id, expected_session_epoch=parent.epoch,
        )
        canonical = await store.get_canonical_transcript(parent.session_id)
        assert [entry.tool_calls[1]["file_snapshot"]["content"] for entry in canonical] == expected
        manager = SessionManager(store, inject_time_prefix=False)
        child = await manager.branch(
            parent.session_key, "agent:main:webchat:file-history-child", fork_transcript=True,
        )
        unrelated = await manager.create("agent:main:webchat:unrelated")
        assert await store.get_canonical_transcript(unrelated.session_id) == []
        await store.delete_session(parent.session_key)
        assert await store.get_canonical_transcript(parent.session_id) == []
        child_history = await store.get_canonical_transcript(child.session_id)
        assert [
            entry.tool_calls[1]["file_snapshot"]["content"] for entry in child_history
        ] == expected
        await store.delete_session(child.session_key)
        assert await store.get_canonical_transcript(child.session_id) == []
    finally:
        await store.close()
