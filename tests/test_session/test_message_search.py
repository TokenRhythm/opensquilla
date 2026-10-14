"""Current-session search stays bounded and survives canonical archiving."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time

import pytest

from opensquilla.compat import aiosqlite
from opensquilla.content_reader import ContentExportLimitError
from opensquilla.history_cursor import HistoryCursorInvalidatedError
from opensquilla.session import message_search
from opensquilla.session.message_search import search_session_messages
from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.recovery_reads import recovery_read_scope
from opensquilla.session.storage import SessionStorage, StorageBusyError

KEY = "agent:main:webchat:find"


@pytest.fixture(params=[False, True], ids=["aiosqlite", "sqlite3-fallback"])
async def storage(tmp_path, monkeypatch, request):
    monkeypatch.setattr(aiosqlite, "_FORCE_SQLITE3_FALLBACK", request.param)
    store = await SessionStorage.open(str(tmp_path / "search.db"))
    await store.upsert_session(SessionNode(session_key=KEY, session_id="sid", agent_id="main"))
    try:
        yield store
    finally:
        await store.close()


async def add(store, text, mid, *, role="assistant", sid="sid", timestamp=10, **kwargs):
    await store.append_transcript_entry(
        TranscriptEntry(
            session_key=KEY,
            session_id=sid,
            message_id=mid,
            role=role,
            content=text,
            created_at=timestamp,
            **kwargs,
        )
    )


async def search(store, query, **kwargs):
    return await search_session_messages(
        store, key=KEY, session_id="sid", session_epoch=0, query=query, **kwargs
    )


async def archive(store, mid, *, delete=True):
    async with store._write_transaction("test_archive") as conn:
        await conn.execute(
            "INSERT INTO compacted_transcript_entries "
            "(session_id, session_key, message_id, role, content, created_at, original_entry_id, "
            "content_byte_length, content_revision, archived_at) "
            "SELECT session_id, session_key, message_id, role, content, created_at, id, "
            "content_byte_length, content_revision, 1 FROM transcript_entries WHERE message_id = ?",
            (mid,),
        )
        if delete:
            await conn.execute("DELETE FROM transcript_entries WHERE message_id = ?", (mid,))


async def test_literal_unicode_code_fences_scope_and_utf16(storage):
    await add(storage, '😀ALPHA 中文 alpha\n```python\nprint("中文")\n```', "match")
    await add(storage, "中文 alpha", "foreign", sid="foreign")
    await add(storage, "中文 alpha", "tool", role="tool")
    await add(storage, "visible body", "thinking", reasoning_content="中文 alpha")
    result = await search(storage, "alpha")
    assert [hit["message_id"] for hit in result["matches"]] == ["match", "match"]
    assert [hit["offset_utf16"] for hit in result["matches"]] == [2, 11]
    assert all(hit["length_utf16"] == 5 for hit in result["matches"])
    assert len((await search(storage, "中文"))["matches"]) == 2
    assert not (await search(storage, ".*"))["matches"]
    assert result["complete"] and result["next_cursor"] is None


async def test_reasoning_only_nullable_body_is_not_searchable_or_unavailable(storage):
    await add(storage, None, "reasoning-only", reasoning_content="private needle")
    result = await search(storage, "needle")
    assert result["complete"] and result["coverage_complete"]
    assert result["unavailable_entries"] == 0 and result["matches"] == []


async def test_display_envelope_excludes_provider_and_attachment_metadata(storage):
    await add(
        storage,
        json.dumps(
            {
                "text": "private provider instruction",
                "display_text": "可见中文",
                "attachments": [{"data": "hidden attachment", "name": "secret"}],
            }
        ),
        "caption",
        role="user",
    )
    assert len((await search(storage, "中文"))["matches"]) == 1
    assert not (await search(storage, "private"))["matches"]
    assert not (await search(storage, "hidden"))["matches"]
    await add(storage, "NO_REPLY", "silent")
    assert not (await search(storage, "NO_REPLY"))["matches"]


async def test_search_uses_display_cleanup_for_legacy_control_and_tool_rows(storage):
    await add(
        storage, "HEARTBEAT_OK all clear", "heartbeat", turn_context={"run_kind": "heartbeat"}
    )
    await add(
        storage,
        "narration\n[Used tool: read_file]\nafter",
        "structured",
        tool_calls=[{"type": "tool_use", "name": "read_file", "input": {"secret": "args"}}],
    )
    await add(storage, "[Used tool: legacy_reader]", "old-call", timestamp=20)
    await add(
        storage,
        "[Tool result (legacy-call): internalpayload]",
        "old-result",
        timestamp=21,
        role="user",
    )
    await add(storage, "ordinary", "separator", timestamp=22, role="user")
    await add(storage, "[Tool result (quoted): userquote]", "quoted", timestamp=23, role="user")
    assert not (await search(storage, "HEARTBEAT_OK"))["matches"]
    assert not (await search(storage, "read_file"))["matches"]
    assert not (await search(storage, "internalpayload"))["matches"]
    assert not (await search(storage, "legacy_reader"))["matches"]
    assert (await search(storage, "userquote"))["matches"][0]["message_id"] == "quoted"
    assert (await search(storage, "after"))["matches"][0]["offset_utf16"] == len("narration\n")


async def test_artifact_envelope_matches_the_visible_text_offsets(storage):
    await add(
        storage,
        json.dumps(
            {
                "text": "before [generated artifact omitted: hidden-report.html (text/html)] after",
                "artifacts": [{"id": "art-search", "name": "report.html"}],
            }
        ),
        "artifact",
    )
    assert not (await search(storage, "hidden-report"))["matches"]
    assert (await search(storage, "after"))["matches"][0]["offset_utf16"] == len("before ")


async def test_archive_keeps_original_cursor_and_duplicate_sources_do_not_duplicate_hits(storage):
    await add(storage, "a matching message", "archived")
    before = await search(storage, "matching")
    await archive(storage, "archived", delete=False)
    assert len((await search(storage, "matching"))["matches"]) == 1
    await storage.conn.execute("DELETE FROM transcript_entries WHERE message_id = ?", ("archived",))
    after = await search(storage, "matching")
    assert after["matches"] == before["matches"]
    # This deliberately constructed old archive has no summary coverage proof.
    assert not after["coverage_complete"]


async def test_archive_between_metadata_and_body_preserves_hit(storage, monkeypatch):
    await add(storage, "needle", "moving")
    original = storage.read_message_search_body

    async def archive_before_body(sid, row):
        await archive(storage, "moving")
        return await original(sid, row)

    monkeypatch.setattr(storage, "read_message_search_body", archive_before_body)
    result = await search(storage, "needle")
    assert result["matches"][0]["message_id"] == "moving"
    assert result["complete"]


async def test_stale_active_revision_cannot_fall_back_to_duplicate_archive(storage):
    await add(storage, "original needle", "edited")
    row = (await storage.read_message_search_rows("sid"))[0]
    await archive(storage, "edited", delete=False)
    await storage.conn.execute(
        "UPDATE transcript_entries SET content = 'new body' WHERE message_id = 'edited'"
    )
    with pytest.raises(HistoryCursorInvalidatedError):
        await storage.read_message_search_body("sid", row)


async def test_empty_scan_pages_advance_and_new_append_is_not_in_frozen_range(storage, monkeypatch):
    monkeypatch.setattr(message_search, "TARGET_PAGE_SECONDS", 10)
    count = message_search.MAX_SCAN_ENTRIES * 2 + 2
    for index in range(count):
        await add(
            storage, "needle" if index == count - 1 else "other body", f"m-{index}", timestamp=index
        )
    first = await search(storage, "needle")
    assert first["matches"] == [] and first["scanned_entries"] == message_search.MAX_SCAN_ENTRIES
    assert not first["complete"] and first["next_cursor"]
    await add(storage, "needle", "newer", timestamp=999)
    second = await search(storage, "needle", cursor=first["next_cursor"])
    assert second["scanned_entries"] == message_search.MAX_SCAN_ENTRIES and second["matches"] == []
    third = await search(storage, "needle", cursor=second["next_cursor"])
    assert third["complete"] and third["scanned_entries"] == 2
    assert [hit["message_id"] for hit in third["matches"]] == [f"m-{count - 1}"]


async def seed_covered_archive(storage, count):
    async with storage._write_transaction("seed_search_coverage") as conn:
        await conn.execute("UPDATE sessions SET compaction_count = 1 WHERE session_id = 'sid'")
        await conn.execute("""
            INSERT INTO session_summaries
                (session_id, session_key, compaction_id, summary_text, removed_count, created_at)
            VALUES ('sid', ?, 'compaction-1', 'summary', ?, 1)
        """, (KEY, count))
        await conn.executemany("""
            INSERT INTO compacted_transcript_entries
                (session_id, session_key, compaction_id, original_entry_id,
                 message_id, role, content, created_at, archived_at)
            VALUES ('sid', ?, 'compaction-1', ?, ?, 'tool', 'hidden', ?, 1)
        """, [(KEY, index + 1, f"archive-{index}", index) for index in range(count)])


async def test_coverage_is_incremental_and_does_not_materialize_bodies_or_count(
    storage, monkeypatch,
):
    count = message_search.MAX_COVERAGE_ROWS * 3
    await seed_covered_archive(storage, count)
    await add(storage, "needle", "current", timestamp=count + 1)
    calls = []
    read = storage.read_message_search_coverage_archives

    async def record(*args, **kwargs):
        assert kwargs["limit"] <= message_search.MAX_COVERAGE_ROWS
        result = await read(*args, **kwargs)
        calls.append(len(result))
        return result

    async def forbidden(*args, **kwargs):
        pytest.fail("Search must not use the unbounded COUNT coverage query")

    monkeypatch.setattr(storage, "read_message_search_coverage_archives", record)
    monkeypatch.setattr(storage, "is_canonical_transcript_complete", forbidden)
    page = await search(storage, "needle")
    assert not page["complete"] and not page["coverage_complete"]
    assert len(page["matches"]) == 1
    assert sum(calls) <= message_search.MAX_COVERAGE_ROWS
    pages = 1
    while not page["complete"]:
        calls.clear()
        page = await search(storage, "needle", cursor=page["next_cursor"])
        assert not page["matches"] and page["scanned_entries"] == 0
        assert sum(calls) <= message_search.MAX_COVERAGE_ROWS
        pages += 1
        assert pages < 20
    assert pages > 3 and page["coverage_complete"]


@pytest.mark.parametrize(
    "corruption", ["missing_id", "summary_count", "removed_count", "new_compaction"],
)
async def test_incremental_coverage_never_claims_missing_or_changed_archive_complete(
    storage, corruption,
):
    await seed_covered_archive(storage, message_search.MAX_COVERAGE_ROWS * 2)
    if corruption == "missing_id":
        await storage.conn.execute(
            "UPDATE compacted_transcript_entries SET original_entry_id=NULL WHERE id=1"
        )
    elif corruption == "summary_count":
        await storage.conn.execute("UPDATE sessions SET compaction_count=2 WHERE session_id='sid'")
    elif corruption == "removed_count":
        await storage.conn.execute("UPDATE session_summaries SET removed_count=removed_count+1")
    page = await search(storage, "absent")
    assert not page["complete"]
    if corruption == "new_compaction":
        await storage.conn.execute("UPDATE sessions SET compaction_count=2 WHERE session_id='sid'")
    for _ in range(20):
        if page["complete"]:
            break
        page = await search(storage, "absent", cursor=page["next_cursor"])
    assert page["complete"] and not page["coverage_complete"]


async def test_search_admission_retains_cpu_lease_after_cancel_without_blocking_writer(
    storage, monkeypatch,
):
    await add(storage, "needle", "first")
    entered, release = threading.Event(), threading.Event()
    original = message_search._project_and_measure

    def held_projection(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(message_search, "_project_and_measure", held_projection)
    task = asyncio.create_task(search(storage, "needle"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(StorageBusyError) as error:
            await search(storage, "other")
        assert error.value.resource == "message_search"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        # The request ended, but its physical CPU job still owns admission.
        with pytest.raises(StorageBusyError):
            await search(storage, "other")
        await asyncio.wait_for(add(storage, "new chat input", "next"), 0.5)
        with recovery_read_scope("foreground-recovery", deadline=time.monotonic() + 1):
            assert await storage.get_session(KEY) is not None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*message_search._RETIRING_SEARCHES)
    assert not message_search._ACTIVE_SEARCHES
    assert (await search(storage, "needle"))["matches"]


async def test_search_admission_retains_native_reader_until_cancel_cleanup_finishes(
    storage, monkeypatch,
):
    await add(storage, "needle", "native")
    entered, release = threading.Event(), threading.Event()
    original_open = storage._open_recovery_reader
    held_once = False

    def gate():
        entered.set()
        assert release.wait(5)
        return 1

    async def open_reader():
        nonlocal held_once
        reader = await original_open()
        await reader.create_function("search_native_gate", 0, gate)
        original_execute = reader.execute

        def execute(sql, parameters=()):
            nonlocal held_once
            if " AS display_bytes," in sql and not held_once:
                held_once = True
                sql = "SELECT * FROM (" + sql + ") WHERE search_native_gate() = 1"
            return original_execute(sql, parameters)

        monkeypatch.setattr(reader, "execute", execute)
        return reader

    monkeypatch.setattr(storage, "_open_recovery_reader", open_reader)
    task = asyncio.create_task(search(storage, "needle"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        for _ in range(10):
            with pytest.raises(StorageBusyError):
                await search(storage, "new query")
        assert storage._recovery_read_pool.active_count == 1
        await asyncio.wait_for(add(storage, "chat continues", "next"), 0.5)
        with recovery_read_scope("different-session-recovery", deadline=time.monotonic() + 1):
            assert await storage.get_session(KEY) is not None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*message_search._RETIRING_SEARCHES)
    assert not message_search._ACTIVE_SEARCHES
    assert storage._recovery_read_pool.active_count == 0
    assert (await search(storage, "needle"))["matches"]


async def test_elapsed_page_budget_yields_at_message_boundary(storage, monkeypatch):
    await add(storage, "needle", "first", timestamp=1)
    await add(storage, "needle", "second", timestamp=2)
    monkeypatch.setattr(message_search, "TARGET_PAGE_SECONDS", 0)
    page = await search(storage, "needle")
    assert page["scanned_entries"] == 1 and not page["complete"]
    assert page["matches"][0]["message_id"] == "first"
    following = await search(storage, "needle", cursor=page["next_cursor"])
    assert following["complete"] and following["matches"][0]["message_id"] == "second"


async def test_compaction_proof_page_seeks_past_unrelated_archives(storage):
    await seed_covered_archive(storage, 4000)
    await storage.conn.execute("""
        INSERT INTO compacted_transcript_entries
            (session_id, session_key, compaction_id, original_entry_id,
             message_id, role, content, created_at, archived_at)
        VALUES ('sid', ?, 'last-compaction', 4001, 'last', 'tool', '', 4001, 1)
    """, (KEY,))
    ticks = 0

    def progress():
        nonlocal ticks
        ticks += 1
        return 0

    reader = storage._transcript_reader
    assert reader is not None
    await reader.set_progress_handler(progress, 100)
    try:
        rows = await storage.read_message_search_coverage_archives(
            "sid", after=0, through=4001, limit=256, compaction_id="last-compaction",
        )
    finally:
        await reader.set_progress_handler(None, 0)
    assert [row["original_entry_id"] for row in rows] == [4001]
    # A session-only scan would examine 4,000 unrelated rows despite LIMIT 256.
    # The existing compaction index makes work depend on this compaction page.
    assert ticks < 50


async def test_occurrence_pages_resume_inside_message_and_survive_compaction(storage):
    await add(storage, "hit hit hit hit hit", "many")
    first = await search(storage, "hit", limit=2)
    await archive(storage, "many")
    second = await search(storage, "hit", limit=2, cursor=first["next_cursor"])
    third = await search(storage, "hit", limit=2, cursor=second["next_cursor"])
    assert [hit["offset_utf16"] for page in (first, second, third) for hit in page["matches"]] == [
        0,
        4,
        8,
        12,
        16,
    ]
    assert third["complete"]


async def test_continuation_is_bound_to_query_and_identity(storage):
    await add(storage, "hit hit", "many")
    first = await search(storage, "hit", limit=1)
    with pytest.raises(ValueError):
        await search(storage, "other", cursor=first["next_cursor"])
    with pytest.raises(ValueError):
        await search_session_messages(
            storage,
            key=KEY,
            session_id="sid",
            session_epoch=1,
            query="hit",
            cursor=first["next_cursor"],
        )
    with pytest.raises(ValueError):
        await search(storage, "hit", cursor="not-a-valid-cursor")
    for changed_identity in ({"key": KEY + "-other"}, {"session_id": "other"}):
        with pytest.raises(ValueError):
            await search_session_messages(
                storage, **{"key": KEY, "session_id": "sid", **changed_identity},
                session_epoch=0, query="hit", cursor=first["next_cursor"],
            )


@pytest.mark.parametrize("key_char, sid_char", [("a", "s"), ("河", "界"), ("😀", "😀")])
async def test_maximum_session_identities_produce_resumable_bounded_cursor(
    storage, key_char, sid_char,
):
    from opensquilla.contracts.generated.v4.sessions_messages_search import Params, Result

    key, sid = key_char * 4096, sid_char * 256
    params = {"key": key, "session_id": sid, "session_epoch": 0, "query": "hit", "limit": 1}
    Params.model_validate(params)
    await storage.upsert_session(SessionNode(session_key=key, session_id=sid, agent_id="main"))
    await storage.append_transcript_entry(TranscriptEntry(
        session_key=key, session_id=sid, message_id="long-identity", role="assistant",
        content="hit hit", created_at=1,
    ))
    first = await search_session_messages(storage, **params)
    Result.model_validate(first)
    assert not first["complete"] and 0 < len(first["next_cursor"]) <= 4096
    following = {**params, "cursor": first["next_cursor"]}
    Params.model_validate(following)
    second = await search_session_messages(storage, **following)
    Result.model_validate(second)
    assert [hit["offset_utf16"] for page in (first, second) for hit in page["matches"]] == [0, 4]
    assert second["complete"] and second["next_cursor"] is None


async def test_content_revision_rejects_offset_continuation_after_edit(storage):
    await add(storage, "hit hit", "edited")
    first = await search(storage, "hit", limit=1)
    await storage.conn.execute(
        "UPDATE transcript_entries SET content='new hit' WHERE message_id='edited'"
    )
    with pytest.raises(HistoryCursorInvalidatedError):
        await search(storage, "hit", cursor=first["next_cursor"])


async def test_byte_budget_stops_before_next_body_and_does_not_truncate_long_tail(storage):
    await add(storage, ("x" * (2 * 1024 * 1024)) + "尾部命中", "large", timestamp=1)
    await add(storage, "尾部命中", "later", timestamp=2)
    first = await search(storage, "尾部命中")
    assert first["scanned_entries"] == 1
    assert first["matches"][0]["offset_utf16"] == 2 * 1024 * 1024
    assert not first["complete"]
    second = await search(storage, "尾部命中", cursor=first["next_cursor"])
    assert second["complete"] and second["matches"][0]["message_id"] == "later"


async def test_unavailable_body_is_explicit_not_a_complete_negative_result(storage, monkeypatch):
    await add(storage, "too large", "large")
    original = storage.read_message_search_body

    async def limited(sid, row):
        return await original(sid, row, max_bytes=3)

    monkeypatch.setattr(storage, "read_message_search_body", limited)
    result = await search(storage, "large")
    assert result["complete"] and not result["coverage_complete"]
    assert result["unavailable_entries"] == 1 and result["matches"] == []
    row = (await storage.read_message_search_rows("sid"))[0]
    with pytest.raises(ContentExportLimitError):
        await original("sid", row, max_bytes=3)


async def test_projection_worker_cancellation_retains_physical_work_until_drain():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def project():
        entered.set()
        try:
            assert release.wait(5)
        finally:
            finished.set()

    with recovery_read_scope("cpu-search", deadline=time.monotonic() + 10) as budget:
        task = asyncio.create_task(message_search._worker(project))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            budget.cancel_token.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not finished.is_set()
        finally:
            release.set()
            await asyncio.wait_for(budget.drain(), 2)
    assert finished.is_set()


@pytest.mark.parametrize("text,query", [
    ("a" * 8191 + "BC" + "a" * 8192, "ab"),
    ("a" * 16385, "aaa"),
    ("x" * 8191 + "😀Alpha" + "x" * 8191 + "😀ALPHA", "😀alpha"),
    ("x" * 8191 + "İıI i" + "x" * 8192, "i"),
    ("x" * 8191 + "ſS s", "s"),
    ("x" * 8191 + "KK k", "k"),
    ("x" * 8191 + "Kk", "K"),
    ("x" * 8191 + "𐐀𐐨", "𐐨"),
    ("Straße STRASSE ß", "ss"),
    ("a" * 8191 + ".[" + "a" * 8192, ".["),
])
def test_bounded_matching_preserves_literal_case_offsets_and_nonoverlap(text, query):
    expected = [
        (len(text[:match.start()].encode("utf-16-le")) // 2,
         len(match.group().encode("utf-16-le")) // 2)
        for match in re.finditer(re.escape(query), text, re.IGNORECASE)
    ]
    found = []
    offset = 0
    while True:
        matches, following = message_search._matches(text, query, offset, 31)
        found.extend((hit["offset_utf16"], hit["length_utf16"]) for hit in matches)
        if following is None:
            break
        assert following > offset
        offset = following
    assert found == expected


def test_ascii_near_match_does_not_enter_regex_engine(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("ASCII near-match should use bounded native literal find")

    monkeypatch.setattr(message_search.re, "compile", forbidden)
    assert message_search._matches("a" * (64 * 1024), "a" * 255 + "b", 0, 10) == ([], None)


async def test_matching_checks_cancel_between_unicode_chunks(monkeypatch):
    with recovery_read_scope("matching-cancel", deadline=time.monotonic() + 2) as budget:
        original_check = budget.check
        checks = 0

        def check():
            nonlocal checks
            checks += 1
            if checks == 2:
                budget.cancel_token.cancel()
            original_check()

        # ReadBudget is slotted; intercept its type method only for this budget.
        original_type_check = type(budget).check
        monkeypatch.setattr(type(budget), "check", lambda self:
                            check() if self is budget else original_type_check(self))
        with pytest.raises(asyncio.CancelledError):
            message_search._matches("α" * (32 * 1024), "α" * 255 + "β", 0, 10)
        assert checks == 2
