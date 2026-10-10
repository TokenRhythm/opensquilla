"""History recovery must preserve display semantics and every page boundary."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.adapters.content_reader import SessionContentReaderStorageAdapter
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage


def test_short_body_with_clipped_details_has_independent_versioned_read_reference():
    entry = SimpleNamespace(
        session_key="agent:main:webchat:details", session_id="sid", message_id="mid",
        role="assistant", content="Complete answer", reasoning_content="思考\n" * 10000,
        tool_calls=[], content_revision="legacy-v1:active:1:1:0:15", content_source="active",
    )
    message = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]
    assert message["text"] == "Complete answer"
    assert message["contentPreviewComplete"] is True
    assert message["historyPayloadPreview"]["detailsTruncated"] is True
    assert message["contentRef"] == {
        "version": 1, "sessionKey": entry.session_key, "sessionId": "sid", "messageId": "mid",
        "view": "display", "byteLength": 15, "source": "active", "revision": entry.content_revision,
    }


@pytest.mark.asyncio
async def test_finalizer_inline_8m_body_fits_v2_page_and_hydrates_without_changing_replay(
    tmp_path, monkeypatch,
):
    """Finalizer stores display text twice; content-only fixtures missed the second copy."""
    body = "answer-" * (8 * 1024 * 1024 // 7)
    body += "x" * (8 * 1024 * 1024 - len(body))
    segments = [{"type": "text", "text": body, "presentation": "answer"}]
    replay = {"version": 1, "messages": [{"role": "assistant", "content": body}]}
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        session = await manager.create("agent:main:webchat:bounded-finalizer")
        await storage.append_transcript_entry(TranscriptEntry(
            session_id=session.session_id, session_key=session.session_key, message_id="answer",
            role="assistant", content=body, tool_calls=segments,
            assistant_replay=replay, created_at=1,
        ))

        async def identity(*args):
            return session.session_id, 1

        monkeypatch.setattr(rpc_sessions, "_snapshot_session_identity", identity)
        page = await rpc_sessions._handle_sessions_history_page_v2({
            "key": session.session_key, "direction": "before", "cursor": None,
            "target_items": 100, "target_bytes": 1048576,
            "projection": {
                "include_content_refs": True, "include_tool_metadata": True,
                "include_outcomes": True,
            },
        }, SimpleNamespace(session_manager=manager, turn_runner=None))
        assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()) <= 1048576
        assert len(page["items"]) == 1
        message = page["items"][0]["message"]
        assert message["contentRef"]["view"] == "display"
        assert message["tool_calls"][0]["presentation"] == "answer"
        assert len(message["tool_calls"][0]["text"].encode()) <= 16384
        assert message["historyPayloadPreview"] == {"textUtf16Lengths": [len(body)]}
        reader = SessionContentReaderStorageAdapter(storage)
        reference = await reader.get_ref(session.session_id, "answer")
        assert await reader.read_display_text(reference) == body
        stored = (await storage.get_transcript(session.session_id))[0]
        assert stored.content == body
        assert stored.tool_calls == segments
        assert stored.assistant_replay == replay
    finally:
        await storage.close()


def test_v2_embedded_history_schema_matches_canonical_projection():
    root = Path(__file__).resolve().parents[2] / "contracts/gateway/v4"
    canonical = json.loads((root / "conversation/chat-history.schema.json").read_text())
    v2 = json.loads((root / "sessions/sessions-history-page-v2.schema.json").read_text())
    # Standalone validators require local definitions; prevent their embedded
    # canonical projection tree from becoming a second, drifting contract.
    for name in ("ChatHistoryMessage", "CompactionSummary", "TurnOutcome", "NullableString",
                 "PageContext", "PageAnnotation", "SelectedSkillRef"):
        assert v2["$defs"][name] == canonical["$defs"][name]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["assistant", "user", "tool"])
@pytest.mark.parametrize("character", ["x", "中", "🙂"])
async def test_normal_bodies_are_projected_completely_before_preview(tmp_path, role, character):
    text = character * 5000 + "END"
    body = json.dumps(
        {"type": "tool_result", "content": text}
        if role == "tool"
        else {"text": text, "artifacts": [{"id": "art-a", "kind": "file", "name": "a.txt"}]},
        ensure_ascii=False,
    )
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.append_transcript_entry(TranscriptEntry(
            session_id="sid", session_key="synthetic", message_id="m", role=role,
            content=body, created_at=1,
        ))
        entries, _ = await storage.get_canonical_transcript_page(
            "sid", limit=10, content_mode="bounded"
        )
        assert entries[0].content == body
        message = transcript_entries_to_chat_messages(entries, content_mode="bounded")[0]
        expected = text.encode()[:16384].decode("utf-8", errors="ignore")
        assert message["text"] == expected
        assert message["contentPreviewComplete"] is (expected == text)
        assert ("contentRef" in message) is (expected != text)
        if role != "tool":
            assert message["artifacts"][0]["id"] == "art-a"
    finally:
        await storage.close()


def test_pending_small_complete_json_is_not_suppressed_and_v2_keeps_semantics():
    entry = TranscriptEntry(session_id="sid", session_key="synthetic", message_id="m",
                            role="assistant", content='{"text":"answer"}', created_at=1)
    object.__setattr__(entry, "content_metadata_pending", True)
    object.__setattr__(entry, "content_truncated", False)
    message = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]
    assert message["text"] == "answer"
    assert message["contentPreviewComplete"] is True
    message["artifacts"] = [{"id": "artifact"}]
    message["tool_calls"] = [{"type": "text", "text": "answer", "presentation": "answer"}]
    item = rpc_sessions._v2_history_item(message, session_id="sid", session_epoch=0, order=0)
    assert item["message"] == message
    assert item["preview_complete"] is True
    assert item["content_availability"] == "preparing"
    assert item["source_revision"]


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["before", "after"])
async def test_byte_limited_pages_do_not_skip_any_message(monkeypatch, direction):
    messages = [dict(message_id=f"m{i}", transcript_id=i, timestamp=i,
                     role="assistant", text="中" * 4090) for i in range(1, 101)]

    async def identity(*args):
        return "sid", 1

    async def history(params, ctx):
        selected = messages
        if params.get("before"):
            selected = [
                m for m in selected if m["transcript_id"] < int(params["before"].split("|")[1])
            ]
        if params.get("after"):
            selected = [
                m for m in selected if m["transcript_id"] > int(params["after"].split("|")[1])
            ]
        return dict(
            messages=selected,
            has_more=False,
            oldest_cursor=f"{selected[0]['timestamp']}|{selected[0]['transcript_id']}"
            if selected
            else None,
            newest_cursor=f"{selected[-1]['timestamp']}|{selected[-1]['transcript_id']}"
            if selected
            else None,
            compaction_summaries=[{"id": "summary"}],
            turn_outcomes=[{"turn_id": "t", "status": "done", "outcome": {}}],
        )

    monkeypatch.setattr(rpc_sessions, "_snapshot_session_identity", identity)
    monkeypatch.setattr(rpc_sessions, "read_chat_history_v4", history)
    params = dict(key="synthetic", direction=direction, target_items=100, target_bytes=1048576)
    seen = []
    while True:
        page = await rpc_sessions._handle_sessions_history_page_v2(params, None)
        assert (
            len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode())
            <= params["target_bytes"]
        )
        assert page["compaction_summaries"] == [{"id": "summary"}]
        seen.extend(item["message_id"] for item in page["items"])
        if not page[f"has_more_{direction}"]:
            break
        assert page["items"]
        params["cursor"] = page[f"{direction}_cursor"]
        assert len(seen) <= 100
    assert len(seen) == len(set(seen)) == 100

    with pytest.raises(rpc_sessions.RpcHandlerError) as error:
        await rpc_sessions._handle_sessions_history_page_v2(
            {**params, "cursor": None, "target_bytes": 1}, None
        )
    assert error.value.code == "RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
@pytest.mark.parametrize("body,truncated", [
    ('{"text":"small complete answer"}', False),
    ("中" * 4096, False),
    ("中" * 4097, True),
], ids=["small-json", "exact-character-bound", "past-character-bound"])
async def test_pending_sql_metadata_is_independent_of_actual_truncation(tmp_path, body, truncated):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.append_transcript_entry(TranscriptEntry(
            session_id="sid", session_key="synthetic", message_id="m", role="assistant",
            content=body, created_at=1,
        ))
        await storage.conn.execute("UPDATE transcript_entries SET content_byte_length = NULL")
        await storage.conn.commit()
        entries, _ = await storage.get_canonical_transcript_page(
            "sid", limit=1, content_mode="bounded"
        )
        assert entries[0].content_truncated is truncated
        assert entries[0].content_metadata_pending is True
        message = transcript_entries_to_chat_messages(entries, content_mode="bounded")[0]
        assert message["text"] == ("small complete answer" if body.startswith("{") else body[:4096])
        assert message["contentPreviewComplete"] is (not truncated)
        assert ("contentRef" in message) is truncated
        if truncated:
            assert message["contentRef"]["view"] == "display"
            assert message["contentRef"]["revision"].endswith(":pending")
            assert "byteLength" not in message["contentRef"]
        item = rpc_sessions._v2_history_item(message, session_id="sid", session_epoch=0, order=0)
        assert item["content_availability"] == "preparing"
        assert item["source_revision"].endswith(":pending")
    finally:
        await storage.close()


@pytest.mark.parametrize("result_only", [False, True])
def test_joined_tool_projection_uses_selected_source_row_boundaries(result_only):
    tool = TranscriptEntry(id=10, session_id="sid", session_key="synthetic", message_id="tool",
                           role="assistant", content="[Used tool: read_file]", created_at=10)
    result = TranscriptEntry(id=11, session_id="sid", session_key="synthetic", message_id="result",
                             role="user", content="[Tool result (call-1): output]", created_at=11)
    messages = transcript_entries_to_chat_messages(
        [result] if result_only else [tool, result], previous_entry=tool if result_only else None,
        content_mode="bounded",
    )
    assert len(messages) == 1
    assert messages[0]["historyCursorBefore"] == ("11|11" if result_only else "10|10")
    assert messages[0]["historyCursorAfter"] == "11|11"


def test_complete_ordinary_json_with_non_string_text_remains_literal():
    entry = TranscriptEntry(role="assistant", content='{"text":42,"result":"ok"}',
                            session_id="sid", session_key="synthetic", message_id="m")
    message = transcript_entries_to_chat_messages([entry], content_mode="bounded")[0]
    assert message["text"] == entry.content
    assert message["contentPreviewComplete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "available,complete,scope,messages",
    [
        (False, False, "complete", []),
        (True, False, "compacted", [{"id": "m", "text": "old archive", "role": "assistant"}]),
        (False, True, "complete", []),
    ],
    ids=[
        "active-fallback-is-not-authoritative",
        "old-archive-remains-readable",
        "confirmed-empty-draft",
    ],
)
async def test_v2_page_preserves_storage_coverage_independent_of_byte_completeness(
    monkeypatch, available, complete, scope, messages,
):
    async def identity(*args):
        return "sid", 1

    async def history(*args):
        return dict(messages=messages, has_more=False, oldest_cursor=None, newest_cursor=None,
                    canonical_available=available, canonical_complete=complete, history_scope=scope)

    monkeypatch.setattr(rpc_sessions, "_snapshot_session_identity", identity)
    monkeypatch.setattr(rpc_sessions, "read_chat_history_v4", history)
    page = await rpc_sessions._handle_sessions_history_page_v2(
        dict(key="synthetic", direction="before"), None
    )
    assert page["canonical_available"] is available
    assert page["canonical_complete"] is complete
    assert page["history_scope"] == scope
    assert page["complete_for_requested_window"] is True
    assert len(page["items"]) == len(messages)
