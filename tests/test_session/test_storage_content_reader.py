"""SQLite integration coverage for the read-only legacy content seam."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from opensquilla.application.content_reader import (
    MAX_CONTENT_RANGE_BYTES,
    ContentExportLimitError,
    ContentMetadataPendingError,
    ContentNotFoundError,
    ContentRangeError,
    LegacyContentRef,
)
from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["native", "fallback"])
@pytest.mark.parametrize("source", ["active", "compacted"])
async def test_unindexed_display_reads_one_versioned_blob_on_its_worker(
    tmp_path, monkeypatch, backend, source,
):
    from opensquilla.compat import aiosqlite
    monkeypatch.setattr(aiosqlite, "_FORCE_SQLITE3_FALLBACK", backend == "fallback")
    monkeypatch.setattr(aiosqlite, "_prefer_native", True)
    if backend == "native":
        pytest.importorskip("aiosqlite")
    events = []
    deny_content = False
    block_read = False
    entered = threading.Event()
    release = threading.Event()

    class Blob:
        def __init__(self, blob): self.blob = blob
        def __len__(self): return len(self.blob)
        def __enter__(self): return self
        def __exit__(self, *_):
            self.blob.close()
            events.append("closed")
        def read(self, size):
            events.append(("read", size, threading.get_ident()))
            if block_read:
                entered.set()
                assert release.wait(5), "test did not release SQLite worker"
            return self.blob.read(size)

    class Connection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.set_authorizer(lambda action, _table, column, *_: sqlite3.SQLITE_DENY
                if deny_content and action == sqlite3.SQLITE_READ and column == "content"
                else sqlite3.SQLITE_OK)
        def blobopen(self, table, column, row, *, readonly=False, **kwargs):
            events.append(("open", table, row, readonly))
            return Blob(super().blobopen(table, column, row, readonly=readonly, **kwargs))

    connect = aiosqlite.connect
    monkeypatch.setattr(
        aiosqlite, "connect", lambda *args, **kwargs: connect(*args, **kwargs, factory=Connection),
    )
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    body = "中文🙂\n" * 4096
    table = "transcript_entries" if source == "active" else "compacted_transcript_entries"
    try:
        await storage.append_transcript_entry(TranscriptEntry(
            session_id="sid", session_key="agent:main:webchat:legacy", message_id="mid",
            role="assistant", content=body, reasoning_content="full thinking", created_at=1,
        ))
        if source == "compacted":
            await storage.conn.execute("""
                INSERT INTO compacted_transcript_entries
                    (id, session_id, session_key, message_id, role, content, reasoning_content,
                     created_at, archived_at, original_entry_id, content_revision)
                SELECT 91, session_id, session_key, message_id, role, content, reasoning_content,
                       created_at, 2, id, content_revision FROM transcript_entries
            """)
            await storage.conn.execute("DELETE FROM transcript_entries")
        await storage.conn.execute(f"UPDATE {table} SET content_byte_length = NULL")
        await storage.conn.commit()
        ref = await storage.get_legacy_content_ref("sid", "mid", source=source, allow_pending=True)
        assert ref.byte_length is None
        assert await storage.read_legacy_display_text(ref) == body
        assert events[0] == ("open", table, 91 if source == "compacted" else 1, True)
        assert events[1][:2] == ("read", len(body.encode()))
        assert events[1][2] != threading.get_ident()
        assert events[-1] == "closed"
        events.clear()
        with pytest.raises(ContentExportLimitError):
            await storage.read_legacy_display_text(ref, max_bytes=100)
        assert len(events) == 2 and events[-1] == "closed"  # No read before rejecting length.
        events.clear()
        deny_content = True
        details = json.loads(await storage.read_legacy_display_details(ref))
        assert details["reasoning_content"] == "full thinking"
        assert events == []
        deny_content = False
        await storage.conn.execute(
            f"UPDATE {table} SET content = ?", (body.replace("中文", "替换"),),
        )
        await storage.conn.commit()
        with pytest.raises(ContentNotFoundError):
            await storage.read_legacy_display_text(ref)
        assert events == []  # Reject the old version before opening any blob.
        # Keep the body unindexed after the ordinary content-update trigger.
        await storage.conn.execute(f"UPDATE {table} SET content_byte_length = NULL")
        await storage.conn.commit()
        current = await storage.get_legacy_content_ref(
            "sid", "mid", source=source, allow_pending=True,
        )
        block_read = True
        task = asyncio.create_task(storage.read_legacy_display_text(current))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        closing = asyncio.create_task(storage.close())
        await asyncio.sleep(0)
        assert not task.done()
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await closing
        assert events[-1] == "closed"
    finally:
        release.set()
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, "", b"\xff"])
async def test_unindexed_display_preserves_empty_and_utf8_validation(tmp_path, body):
    from opensquilla.content_reader import ContentEncodingError

    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.append_transcript_entry(TranscriptEntry(
            session_id="sid", message_id="mid", role="assistant", content="seed", created_at=1,
        ))
        await storage.conn.execute("UPDATE transcript_entries SET content = ?", (body,))
        await storage.conn.execute("UPDATE transcript_entries SET content_byte_length = NULL")
        await storage.conn.commit()
        ref = await storage.get_legacy_content_ref("sid", "mid", allow_pending=True)
        if isinstance(body, bytes):
            with pytest.raises(ContentEncodingError):
                await storage.read_legacy_display_text(ref)
        else:
            assert await storage.read_legacy_display_text(ref) == ""
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["active", "compacted"])
async def test_history_details_restore_without_reading_or_replacing_body(tmp_path, source):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    reasoning = "thinking 中文\n" * 4000
    segments = [
        {"type": "text", "text": "Complete answer", "activity_order": 1},
        {"type": "tool_use", "id": "tool-1", "name": "write_file",
         "input": {"path": "page.html", "content": "<html>中文</html>" * 3000},
         "activity_order": 2},
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "result\n" * 5000,
         "activity_order": 3},
    ]
    try:
        await storage.append_transcript_entry(TranscriptEntry(
            session_id="sid", session_key="agent:main:webchat:details", message_id="mid",
            role="assistant", content="Complete answer", reasoning_content=reasoning,
            tool_calls=segments, created_at=1,
        ))
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
            await storage.conn.commit()
        ref = await storage.get_legacy_content_ref("sid", "mid", source=source)
        details = json.loads(await storage.read_legacy_display_details(ref))
        assert details["reasoning_content"] == reasoning
        assert details["tool_calls"][0]["text"] == "Complete answer"
        assert details["tool_calls"][1]["input"] == segments[1]["input"]
        assert details["tool_calls"][2]["content"] == segments[2]["content"]
        assert [s["activity_order"] for s in details["tool_calls"]] == [1, 2, 3]
        assert "text" not in details
        assert "assistant_replay" not in details
        with pytest.raises(ContentExportLimitError):
            await storage.read_legacy_display_details(ref, max_bytes=1024)
        table = "transcript_entries" if source == "active" else "compacted_transcript_entries"
        await storage.conn.execute(
            f"UPDATE {table} SET reasoning_content = ? WHERE message_id = ?",
            (reasoning.replace("thinking", "replaced"), "mid"),
        )
        await storage.conn.commit()
        with pytest.raises(ContentNotFoundError):
            await storage.read_legacy_display_details(ref)
        current = await storage.get_legacy_content_ref("sid", "mid", source=source)
        assert current.revision != ref.revision
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_legacy_content_range_reads_only_requested_bytes(tmp_path) -> None:
    body = '{"text":"' + ("x你好" * 350_000) + '"}'
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id="sid",
                session_key="agent:main:webchat:default",
                message_id="mid",
                role="assistant",
                content=body,
                created_at=1,
            )
        )
        ref = await storage.get_legacy_content_ref("sid", "mid")
        assert ref.source == "active"
        assert ref.byte_length == len(body.encode())

        first = await storage.read_legacy_content_range(ref, offset=0, limit=17)
        second = await storage.read_legacy_content_range(ref, offset=17, limit=31)
        encoded = body.encode()
        assert first.data == encoded[:17]
        assert second.data == encoded[17:48]
        assert len(first.data) <= first.limit
        assert len(second.data) <= second.limit

        digest = hashlib.sha256()
        offset = 0
        while offset < ref.byte_length:
            part = await storage.read_legacy_content_range(
                ref, offset=offset, limit=64 * 1024
            )
            digest.update(part.data)
            offset = part.end
        assert digest.hexdigest() == hashlib.sha256(encoded).hexdigest()

        with pytest.raises(ContentNotFoundError, match="changed"):
            await storage.read_legacy_content_range(
                replace(ref, byte_length=ref.byte_length + 1),
                offset=0,
                limit=17,
            )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_legacy_content_range_rejects_same_length_row_replacement(tmp_path) -> None:
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id="sid",
                session_key="agent:main:webchat:default",
                message_id="mid",
                role="user",
                content="original body",
                created_at=1,
            )
        )
        ref = await storage.get_legacy_content_ref("sid", "mid")
        await storage.conn.execute(
            "UPDATE transcript_entries SET content = ? WHERE session_id = ? AND message_id = ?",
            ("replaced body", "sid", "mid"),
        )
        await storage.conn.commit()
        with pytest.raises(ContentNotFoundError, match="changed"):
            await storage.read_legacy_content_range(ref, offset=0, limit=32)
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["active", "compacted"])
@pytest.mark.parametrize("view", ["display", "details"])
async def test_display_read_rejects_same_length_update_after_ref_check(
    tmp_path, monkeypatch, source: str, view: str,
) -> None:
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.append_transcript_entry(TranscriptEntry(
            session_id="sid", session_key="agent:main:webchat:default", message_id="mid",
            role="assistant", content="original body", reasoning_content="original body",
            created_at=1,
        ))
        if source == "compacted":
            entry = (await storage.get_transcript("sid"))[0]
            await storage._archive_transcript_entries(
                node=SimpleNamespace(session_id="sid"), entries=[entry],
                compaction_id="display-race", compaction_index=0, source_rows_validated=True,
            )
            await storage.conn.execute("DELETE FROM transcript_entries WHERE id = ?", (entry.id,))
            await storage.conn.commit()
        ref = await storage.get_legacy_content_ref("sid", "mid", source=source)
        read_query = storage._read_history_query
        replaced = False

        async def replace_before_display(sql, params, *, operation, **kwargs):
            nonlocal replaced
            if operation == f"content_{view}" and not replaced:
                replaced = True
                table = (
                    "transcript_entries" if source == "active" else "compacted_transcript_entries"
                )
                column = "content" if view == "display" else "reasoning_content"
                await storage.conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE session_id = ? AND message_id = ?",
                    ("replaced body", "sid", "mid"),
                )
                await storage.conn.commit()
            return await read_query(sql, params, operation=operation, **kwargs)

        monkeypatch.setattr(storage, "_read_history_query", replace_before_display)
        read = (storage.read_legacy_display_text if view == "display"
                else storage.read_legacy_display_details)
        with pytest.raises(ContentNotFoundError):
            await read(ref)
        current = await storage.get_legacy_content_ref("sid", "mid", source=source)
        assert current.revision != ref.revision
        assert current.byte_length == ref.byte_length
        result = await read(current)
        assert (result if view == "display" else json.loads(result)["reasoning_content"]) == (
            "replaced body"
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_legacy_content_metadata_backfill_is_bounded_and_restartable(tmp_path) -> None:
    body = "你好" * 128
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id="sid",
                session_key="agent:main:webchat:default",
                message_id="pending",
                role="assistant",
                content=body,
                created_at=1,
            )
        )
        await storage.conn.execute(
            "UPDATE transcript_entries SET content_byte_length = NULL "
            "WHERE session_id = ? AND message_id = ?",
            ("sid", "pending"),
        )
        await storage.conn.commit()

        with pytest.raises(ContentMetadataPendingError):
            await storage.get_legacy_content_ref("sid", "pending")

        assert await storage.backfill_transcript_content_lengths(
            batch_size=1, max_batches=1
        ) == 1
        ref = await storage.get_legacy_content_ref("sid", "pending")
        assert ref.byte_length == len(body.encode("utf-8"))
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_legacy_content_reader_supports_empty_and_missing_entries(tmp_path) -> None:
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id="sid",
                session_key="agent:main:webchat:default",
                message_id="empty",
                role="assistant",
                content="",
                created_at=1,
            )
        )
        ref = await storage.get_legacy_content_ref("sid", "empty")
        assert ref.byte_length == 0
        eof = await storage.read_legacy_content_range(ref, offset=0, limit=1)
        assert eof.data == b""
        assert eof.eof
        with pytest.raises(ContentRangeError):
            await storage.read_legacy_content_range(ref, offset=1, limit=1)
        with pytest.raises(ContentNotFoundError):
            await storage.get_legacy_content_ref("sid", "missing")
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_legacy_content_range_cap_is_enforced_before_sql(tmp_path) -> None:
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        with pytest.raises(ContentRangeError):
            await storage.read_legacy_content_range(
                # The ref is synthetic; validation must happen before any
                # database query and therefore before the missing row check.
                ref=LegacyContentRef("sid", "mid", "active", 0),
                limit=MAX_CONTENT_RANGE_BYTES + 1,
            )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_semantic_content_reader_reapplies_history_projection(tmp_path) -> None:
    body = '{"text":"' + ("x" * (20 * 1024)) + '","display_text":"shown"}'
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id="sid",
                session_key="agent:main:webchat:default",
                message_id="projected",
                role="assistant",
                content=body,
                created_at=1,
            )
        )
        ref = await storage.get_legacy_content_ref("sid", "projected")
        assert await storage.read_legacy_display_text(ref) == "shown"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_semantic_content_reader_projects_tool_result_json(tmp_path) -> None:
    body = '{"type":"tool_result","tool_use_id":"call-1","content":"safe output"}'
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id="sid",
                session_key="agent:main:webchat:default",
                message_id="tool-json",
                role="tool",
                content=body,
                created_at=1,
            )
        )
        ref = await storage.get_legacy_content_ref("sid", "tool-json")
        assert await storage.read_legacy_display_text(ref) == "safe output"
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unindexed", [False, True])
async def test_semantic_content_reader_rejects_large_raw_rows_before_projection(
    tmp_path, unindexed,
) -> None:
    body = "x" * (MAX_CONTENT_RANGE_BYTES * 8 + 1)
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id="sid",
                session_key="agent:main:webchat:default",
                message_id="too-large",
                role="assistant",
                content=body,
                created_at=1,
            )
        )
        if unindexed:
            await storage.conn.execute("UPDATE transcript_entries SET content_byte_length = NULL")
            await storage.conn.commit()
        ref = await storage.get_legacy_content_ref("sid", "too-large", allow_pending=unindexed)
        with pytest.raises(ContentExportLimitError):
            await storage.read_legacy_display_text(ref)
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_bounded_canonical_history_projects_small_envelope_without_extra_read(
    tmp_path,
) -> None:
    body = '{"text":"' + ("x" * 20_000) + '","display_text":"shown"}'
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id="sid",
                session_key="agent:main:webchat:default",
                message_id="canonical-projected",
                role="assistant",
                content=body,
                created_at=1,
            )
        )
        entries, _ = await storage.get_canonical_transcript_page(
            "sid", limit=1, content_mode="bounded"
        )
        message = transcript_entries_to_chat_messages(entries, content_mode="bounded")[0]
        assert message["text"] == "shown"
        assert message["contentPreviewComplete"] is True
        assert "contentRef" not in message
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("read_path", ["active", "canonical", "compacted"])
async def test_bounded_tool_envelope_keeps_semantic_read_reference(
    tmp_path, read_path: str
) -> None:
    displayed = "safe output 中文\n" * 2000 + "TOOL-END"
    body = json.dumps({
        "type": "tool_result", "tool_use_id": "internal-call-id", "content": displayed,
    }, ensure_ascii=False)
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.append_transcript_entry(TranscriptEntry(
            session_id="sid", session_key="agent:main:webchat:default", message_id="tool-json",
            role="tool", content=body, created_at=1,
        ))
        if read_path == "compacted":
            entry = (await storage.get_transcript("sid"))[0]
            await storage._archive_transcript_entries(
                node=SimpleNamespace(session_id="sid"), entries=[entry],
                compaction_id="tool-compaction", compaction_index=0, source_rows_validated=True,
            )
            await storage.conn.execute("DELETE FROM transcript_entries WHERE id = ?", (entry.id,))
            await storage.conn.commit()
        if read_path == "active":
            entries = await storage.get_transcript("sid", content_mode="bounded")
        else:
            entries, _ = await storage.get_canonical_transcript_page(
                "sid", limit=1, content_mode="bounded"
            )
        assert entries[0].content == body
        assert not entries[0].content_truncated
        message = transcript_entries_to_chat_messages(entries, content_mode="bounded")[0]
        assert message["text"] == displayed.encode()[:16 * 1024].decode("utf-8", errors="ignore")
        assert message["contentPreviewComplete"] is False
        assert message["contentRef"]["view"] == "display"
        assert message["contentRef"]["source"] == (
            "compacted" if read_path == "compacted" else "active"
        )
        assert "contentUnavailableReason" not in message
        ref = await storage.get_legacy_content_ref("sid", "tool-json")
        assert message["contentRef"]["revision"] == ref.revision
        text = await storage.read_legacy_display_text(ref)
        assert text == displayed
        assert "internal-call-id" not in text
        assert "tool_result" not in text
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "x" * (128 * 1024),
    json.dumps({"text": "x" * 20_000}),
    '{"metadata":{"kind":"ordinary","version":1},"text":"' + "x" * 20_000,
    json.dumps({"metadata": {"kind": "ordinary", "version": 1}, "text": "x" * 70_000}),
], ids=["plain", "ordinary-json", "incomplete-json", "oversized-json"])
async def test_bounded_storage_parse_budget_is_independent_of_envelope_kind(
    tmp_path, body: str
) -> None:
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        await storage.append_transcript_entry(TranscriptEntry(
            session_id="sid", session_key="agent:main:webchat:default", message_id="ordinary",
            role="assistant", content=body, created_at=1,
        ))
        active = await storage.get_transcript("sid", content_mode="bounded")
        canonical, _ = await storage.get_canonical_transcript_page(
            "sid", limit=1, content_mode="bounded"
        )
        expected = body if len(body.encode()) <= 64 * 1024 else body[:4096]
        assert active[0].content == canonical[0].content == expected
        assert active[0].content_truncated is (expected != body)
        assert canonical[0].content_truncated is (expected != body)
    finally:
        await storage.close()
