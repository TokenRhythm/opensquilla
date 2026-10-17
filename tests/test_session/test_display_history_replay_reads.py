"""Display history excludes internal replay; detail recovery reads one bounded row."""

import json
import threading

import pytest

from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage


@pytest.mark.asyncio
async def test_display_projection_skips_replay_without_changing_default_reads(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        node = await manager.create("agent:main:display-replay")
        replay = {"version": 1, "messages": [{"role": "assistant", "content": "private"}]}
        entry = await manager.append_message(
            node.session_key, "assistant", "visible", assistant_replay=replay,
        )
        default_page, _ = await storage.get_canonical_transcript_page(node.session_id, limit=5)
        assert default_page[0].assistant_replay == replay
        assert (await storage.get_transcript(node.session_id))[0].assistant_replay == replay

        # A malformed provider envelope must not be loaded/decoded while paging
        # display history. Provider reads retain their existing strict contract.
        await storage.conn.execute(
            "UPDATE transcript_entries SET assistant_replay = ? WHERE message_id = ?",
            ("{invalid-private-replay", entry.message_id),
        )
        await storage.conn.commit()
        manager_page = await manager.get_canonical_transcript_page(
            node.session_key, limit=5, content_mode="bounded",
        )
        page, more = manager_page.entries, manager_page.has_more
        assert not more
        assert [(item.content, item.assistant_replay) for item in page] == [("visible", None)]
        ref = await storage.get_legacy_content_ref(
            node.session_id, entry.message_id, allow_pending=True,
        )
        single = await storage.read_legacy_display_entry(ref)
        assert single is not None and single.content == "visible"
        assert single.assistant_replay is None
        active = await manager.get_transcript(node.session_key, content_mode="bounded")
        assert active[0].content == "visible" and active[0].assistant_replay is None
        with pytest.raises(ValueError, match="assistant replay"):
            await storage.get_transcript(node.session_id)
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("archived", [False, True])
async def test_exact_replay_read_is_session_scoped_and_decodes_off_event_loop(
    tmp_path, monkeypatch, archived,
):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        node = await manager.create("agent:main:exact-replay")
        foreign = await manager.create("agent:main:foreign-replay")
        replay = {"version": 1, "messages": [{"content": "saved file text"}]}
        entry = await manager.append_message(
            node.session_key, "assistant", "display", assistant_message_id="same-message",
            assistant_replay=replay,
        )
        await manager.append_message(
            foreign.session_key, "assistant", "foreign", assistant_message_id="same-message",
            assistant_replay={"messages": [{"content": "wrong session"}]},
        )
        if archived:
            await manager.append_message(node.session_key, "user", "next")
            await manager.persist_compaction_result(
                node.session_key, "summary", [{"role": "user", "content": "next"}],
                compaction_id="exact-replay-compaction",
            )
            page, _ = await storage.get_canonical_transcript_page(
                node.session_id, limit=5, content_mode="bounded",
            )
            assert page[0].message_id == entry.message_id
            assert page[0].assistant_replay is None
        event_loop_thread = threading.get_ident()
        original_loads = json.loads
        parse_threads = []

        def tracked_loads(raw, *args, **kwargs):
            parse_threads.append(threading.get_ident())
            return original_loads(raw, *args, **kwargs)

        monkeypatch.setattr(json, "loads", tracked_loads)
        assert await storage.get_canonical_transcript_replay(
            node.session_id, entry.message_id, max_bytes=4096,
        ) == replay
        assert parse_threads and event_loop_thread not in parse_threads
        assert await storage.get_canonical_transcript_replay(
            "missing-session", entry.message_id, max_bytes=4096,
        ) is None
        assert await storage.get_canonical_transcript_replay(
            node.session_id, "missing-message", max_bytes=4096,
        ) is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_oversized_replay_is_rejected_in_sql_before_transfer_or_decode(tmp_path, monkeypatch):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        node = await manager.create("agent:main:bounded-replay")
        entry = await manager.append_message(node.session_key, "assistant", "display")
        # The limit is UTF-8 bytes, not characters. Store raw JSON to make the
        # byte boundary independent of the writer's escaping preference.
        raw = json.dumps({"content": "正文" * 64}, ensure_ascii=False)
        await storage.conn.execute(
            "UPDATE transcript_entries SET assistant_replay = ? WHERE message_id = ?",
            (raw, entry.message_id),
        )
        await storage.conn.commit()
        original_read = storage._read_history_query
        returned_payloads = []

        async def track_read(sql, params):
            rows = await original_read(sql, params)
            returned_payloads.extend(row["assistant_replay"] for row in rows)
            return rows

        def fail_decode(*args, **kwargs):
            pytest.fail("oversized replay must not reach the JSON decoder")

        monkeypatch.setattr(storage, "_read_history_query", track_read)
        monkeypatch.setattr(json, "loads", fail_decode)
        assert await storage.get_canonical_transcript_replay(
            node.session_id, entry.message_id, max_bytes=len(raw),
        ) is None
        assert returned_payloads == [None]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_exact_message_queries_have_session_message_indexes(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        for table, key in (
            ("transcript_entries", "id"),
            ("compacted_transcript_entries", "original_entry_id"),
        ):
            rows = await storage._read_history_query(
                f"EXPLAIN QUERY PLAN SELECT assistant_replay FROM {table} "
                f"WHERE session_id = ? AND message_id = ? ORDER BY created_at DESC, {key} DESC "
                "LIMIT 1", ("session", "message"),
            )
            details = " ".join(str(row["detail"]) for row in rows)
            assert "session_message" in details
            assert "session_id=? AND message_id=?" in details
            assert "SCAN" not in details
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_large_history_page_never_decodes_private_replay(tmp_path, monkeypatch):
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        node = await manager.create("agent:main:large-display-history")
        replay = json.dumps({"private_replay": "x" * (16 * 1024)})
        await storage.conn.executemany(
            "INSERT INTO transcript_entries "
            "(session_id, session_key, message_id, role, content, assistant_replay, created_at) "
            "VALUES (?, ?, ?, 'assistant', 'Visible summary', ?, ?)",
            [
                (node.session_id, node.session_key, f"message-{index:04d}", replay, index + 1)
                for index in range(1000)
            ],
        )
        await storage.conn.commit()
        original_loads = json.loads

        def forbid_private_decode(raw, *args, **kwargs):
            assert raw != replay, "display history must not decode any private replay"
            return original_loads(raw, *args, **kwargs)

        monkeypatch.setattr(json, "loads", forbid_private_decode)
        page = await manager.get_canonical_transcript_page(
            node.session_key, limit=100, content_mode="bounded",
        )
        assert len(page.entries) == 100 and page.has_more
        assert page.entries[0].message_id == "message-0900"
        assert page.entries[-1].message_id == "message-0999"
        assert all(entry.assistant_replay is None for entry in page.entries)
    finally:
        await storage.close()
