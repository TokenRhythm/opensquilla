"""Large transcript previews preserve full-text matches and small-row output."""

from collections.abc import AsyncIterator

import pytest

from opensquilla.compat import aiosqlite
from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage

KEY = "agent:main:webchat:search-excerpts"


@pytest.fixture(params=[False, True], ids=["aiosqlite", "sqlite3-fallback"])
async def storage(tmp_path, monkeypatch, request) -> AsyncIterator[SessionStorage]:
    monkeypatch.setattr(aiosqlite, "_FORCE_SQLITE3_FALLBACK", request.param)
    result = await SessionStorage.open(tmp_path / "search.db")
    await result.upsert_session(SessionNode(session_key=KEY, session_id="sid", agent_id="main"))
    try:
        yield result
    finally:
        await result.close()


async def add(storage: SessionStorage, body: str, mid: str) -> None:
    await storage.append_transcript_entry(TranscriptEntry(
        session_key=KEY, session_id="sid", message_id=mid, role="user", content=body,
        created_at=100,
    ), expected_epoch=0)


async def test_large_tail_match_keeps_native_rank_and_short_snippet(
    storage: SessionStorage,
) -> None:
    await add(storage, "prefix Alpha short BETA suffix", "short")
    tail = ("filler " * 150_000) + "tail Alpha distinct BETA ending"
    await add(storage, tail, "large")
    await add(storage, "Alpha only", "missing-term")
    query = '"alpha" "beta"'
    async with storage.conn.execute(
        "SELECT t.id FROM transcript_fts f JOIN transcript_entries t ON f.rowid=t.id "
        "WHERE f.content MATCH ? ORDER BY f.rank LIMIT 20", [query],
    ) as cursor:
        expected_ids = [row[0] for row in await cursor.fetchall()]
    async with storage.conn.execute(
        "SELECT snippet(transcript_fts, 0, '>>>', '<<<', '...', 48) "
        "FROM transcript_fts WHERE content MATCH ? AND rowid=1", [query],
    ) as cursor:
        short_snippet = (await cursor.fetchone())[0]
    results = await storage.search_transcript("aLpHa beta", session_id="sid")
    assert [row["id"] for row in results] == expected_ids
    assert next(row["snippet"] for row in results if row["id"] == 1) == short_snippet
    large_snippet = next(row["snippet"] for row in results if row["id"] == 2)
    assert large_snippet == storage._make_snippet(tail, "alpha")
    assert len(large_snippet) < 120
    assert await storage.search_transcript("alpha beta", session_id="other") == []
    assert len(await storage.search_transcript("alpha beta", limit=1)) == 1
    # Legacy rows without byte metadata must use the same safe path.
    await storage.conn.execute("UPDATE transcript_entries SET content_byte_length=NULL WHERE id=2")
    assert next(row["snippet"] for row in await storage.search_transcript("alpha beta")
                if row["id"] == 2) == large_snippet


@pytest.mark.parametrize("where", ["start", "middle", "end"])
async def test_large_literal_window_and_ellipsis(storage: SessionStorage, where: str) -> None:
    filler = "varied filler " * 85_000
    hit = "BegInMarker token EndMarker "
    body = {"start": hit + filler, "middle": filler + hit + filler, "end": filler + hit}[where]
    await add(storage, body, "body")
    result = await storage.search_transcript("beginmarker endmarker")
    assert result[0]["snippet"] == storage._make_snippet(body, "beginmarker")


async def test_fts_normalized_match_without_literal_is_not_lost(storage: SessionStorage) -> None:
    await add(storage, "opening preview " + "filler " * 150_000 + "CAFÉ", "accent")
    results = await storage.search_transcript("cafe")
    assert len(results) == 1
    assert results[0]["snippet"].startswith("opening preview")
    assert len(results[0]["snippet"]) == 80


@pytest.mark.parametrize("query,hit", [("部署 ALPHA", "部署 Alpha"), ("ÉCOLE 部署", "École 部署")])
async def test_like_tail_excerpt_preserves_unicode_case_and_all_terms(storage, query, hit) -> None:
    body = "opening " + "多语言 text " * 150_000 + hit + " ending"
    await add(storage, body, "unicode")
    await add(storage, "部署 only", "missing-term")
    results = await storage.search_transcript_like(query)
    assert len(results) == 1
    assert results[0]["snippet"] == storage._make_snippet(body, query.split()[0])
    assert len(results[0]["snippet"]) < 120


async def test_like_projects_excerpts_only_for_final_limited_rows(storage, monkeypatch) -> None:
    for index in range(40):
        await storage.append_transcript_entry(TranscriptEntry(
            session_key=KEY, session_id="sid", message_id=f"ordered-{index}",
            role="user", content=f"检索正文 {index} " + "x" * 8192, created_at=index,
        ), expected_epoch=0)

    excerpts = []

    def record_excerpt(value):
        excerpts.append(value)
        return value

    reader = storage._transcript_reader or storage.conn
    await reader.create_function("record_excerpt", 1, record_excerpt)
    original = storage._search_excerpt_sql
    monkeypatch.setattr(storage, "_search_excerpt_sql", lambda content, folded:
                        f"record_excerpt({original(content, folded)})")
    results = await storage.search_transcript_like("检索正文", limit=5)
    assert [row["created_at"] for row in results] == [39, 38, 37, 36, 35]
    # Increasing insertion timestamps continuously replace the sort's top K.
    # Discarded candidates must not evaluate their expensive body excerpts.
    assert len(excerpts) == len(results) == 5


@pytest.mark.parametrize("query", [
    "部署 ALPHA", "ÉCOLE 部署", "中文%_\\", "检索正文", "\0", "检索\0正文",
])
async def test_like_limited_projection_matches_original_query(storage, query) -> None:
    other_key = "agent:main:webchat:search-excerpts-other"
    await storage.upsert_session(SessionNode(
        session_key=other_key, session_id="other", agent_id="main",
    ))
    bodies = [
        "检索正文 部署 Alpha École 中文%_\\ 完成",
        "before\0检索正文 部署 ALPHA ÉCOLE 中文%_\\",
        "检索正文 部署 alpha école 中文%_\\\0after",
        "检索正文 部署 only 中文-anything",
        "检索正文 部署 ALPHA ÉCOLE 中文%_\\ end",
        "missing all terms",
    ]
    for index, body in enumerate(bodies):
        for sid, key in [("sid", KEY), ("other", other_key)]:
            await storage.append_transcript_entry(TranscriptEntry(
                session_key=key, session_id=sid, message_id=f"{sid}-{index}",
                role="user", content=body, created_at=index,
            ), expected_epoch=0)

    tokens = storage._like_tokens(query)
    col = "py_lower(content)" if storage._needs_unicode_fold(query) else "content"
    first = query.split()[0]
    folded = "py_lower(content)" if storage._needs_unicode_fold(first) else "lower(content)"
    excerpt = storage._search_excerpt_sql("content", folded)
    for sid in [None, "sid", "other", "missing"]:
        for limit in [0, 1, 3, 20]:
            where = " AND ".join(f"{col} LIKE ? ESCAPE '\\'" for _ in tokens)
            params = [first.lower(), first.lower(), len(first) + 81, *tokens]
            if sid:
                where += " AND session_id = ?"
                params.append(sid)
            params.append(limit)
            async with storage.conn.execute(
                f"SELECT id, session_key, role, {excerpt} AS content, created_at "
                f"FROM transcript_entries WHERE {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ) as cursor:
                expected = []
                for row in await cursor.fetchall():
                    value = dict(row)
                    value["snippet"] = storage._make_snippet(str(value.pop("content") or ""), first)
                    expected.append(value)
            actual = await storage.search_transcript_like(query, session_id=sid, limit=limit)
            assert actual == expected


async def test_like_equal_timestamps_keep_selected_candidates(storage) -> None:
    for index in range(8):
        await add(storage, f"检索正文 {index}", f"same-time-{index}")
    async with storage.conn.execute(
        "SELECT id, created_at FROM transcript_entries "
        "WHERE content LIKE ? ESCAPE '\\' AND session_id = ? "
        "ORDER BY created_at DESC LIMIT ?", ["%检索正文%", "sid", 3],
    ) as cursor:
        expected = await cursor.fetchall()
    actual = await storage.search_transcript_like("检索正文", session_id="sid", limit=3)
    # The existing ORDER BY does not promise a tie-break for equal timestamps.
    # Preserve the selected rows and timestamp ordering, without adding one.
    assert {row["id"] for row in actual} == {row["id"] for row in expected}
    assert [row["created_at"] for row in actual] == [row["created_at"] for row in expected]
