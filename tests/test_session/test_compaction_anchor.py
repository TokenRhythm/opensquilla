"""Tests for D12: Compaction Anchor mechanism.

Verifies anchor generation, parsing, exact-lookup retrieval, migration,
and end-to-end compaction → anchor → expand flow.
"""

from __future__ import annotations

import json

import pytest

from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    _format_chunk_for_llm,
    compact_context,
    extract_anchors_from_summary,
)
from opensquilla.session.models import (
    SessionNode,
    SessionSummary,
    TranscriptEntry,
)
from opensquilla.session.storage import SessionStorage


# ── Helpers ──────────────────────────────────────────────────────────────


def _entry(
    session_id: str,
    session_key: str,
    role: str,
    content: str,
    *,
    created_at: int = 1000,
) -> TranscriptEntry:
    return TranscriptEntry(
        session_id=session_id,
        session_key=session_key,
        role=role,
        content=content,
        created_at=created_at,
    )


def _node(session_key: str, session_id: str) -> SessionNode:
    return SessionNode(session_key=session_key, session_id=session_id)


async def _setup_storage(tmp_path) -> SessionStorage:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    return storage


# ── Unit: extract_anchors_from_summary ───────────────────────────────────


class TestExtractAnchorsFromSummary:
    def test_single_anchor(self):
        text = "用户确认了设计 [anchor:2:entry_005]"
        result = extract_anchors_from_summary(text)
        assert result == [{"compaction_index": 2, "entry_anchor_id": "entry_005"}]

    def test_multiple_anchors(self):
        text = (
            "First point [anchor:0:entry_001]. "
            "Second point [anchor:0:entry_003]. "
            "Third [anchor:1:entry_000]."
        )
        result = extract_anchors_from_summary(text)
        assert len(result) == 3
        assert result[0] == {"compaction_index": 0, "entry_anchor_id": "entry_001"}
        assert result[1] == {"compaction_index": 0, "entry_anchor_id": "entry_003"}
        assert result[2] == {"compaction_index": 1, "entry_anchor_id": "entry_000"}

    def test_deduplication(self):
        text = "Same ref [anchor:0:entry_001] and again [anchor:0:entry_001]"
        result = extract_anchors_from_summary(text)
        assert len(result) == 1

    def test_no_anchors(self):
        assert extract_anchors_from_summary("A plain summary.") == []

    def test_malformed_anchors_ignored(self):
        text = "Bad [anchor:abc:entry_001] and [anchor:0:bad_id] and good [anchor:0:entry_002]"
        result = extract_anchors_from_summary(text)
        assert len(result) == 1
        assert result[0]["entry_anchor_id"] == "entry_002"

    def test_empty_string(self):
        assert extract_anchors_from_summary("") == []

    def test_different_compaction_indices_are_distinct(self):
        text = "[anchor:0:entry_001] and [anchor:1:entry_001]"
        result = extract_anchors_from_summary(text)
        assert len(result) == 2
        assert result[0]["compaction_index"] == 0
        assert result[1]["compaction_index"] == 1


# ── Unit: _format_chunk_for_llm with anchors ─────────────────────────────


class TestFormatChunkWithAnchors:
    def test_without_anchors_backward_compat(self):
        chunk = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = _format_chunk_for_llm(chunk)
        assert "[user]:" in result
        assert "[assistant]:" in result
        assert "entry_" not in result

    def test_with_anchors(self):
        chunk = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = _format_chunk_for_llm(chunk, include_anchors=True)
        assert "[entry_000 | user]:" in result
        assert "[entry_001 | assistant]:" in result

    def test_anchor_base_offset(self):
        chunk = [{"role": "user", "content": "test"}]
        result = _format_chunk_for_llm(chunk, anchor_base=5, include_anchors=True)
        assert "[entry_005 | user]:" in result

    def test_content_preserved(self):
        chunk = [{"role": "user", "content": "important decision about architecture"}]
        result = _format_chunk_for_llm(chunk, include_anchors=True)
        assert "important decision about architecture" in result


# ── Integration: migration adds columns ──────────────────────────────────


@pytest.mark.asyncio
async def test_migration_adds_anchor_columns(tmp_path) -> None:
    """connect() idempotently adds compaction_anchor_id and extracted_anchors."""
    storage = await _setup_storage(tmp_path)
    try:
        async with storage.conn.execute(
            "PRAGMA table_info(compacted_transcript_entries)"
        ) as cur:
            cols = {row[1] for row in await cur.fetchall()}
        assert "compaction_anchor_id" in cols

        async with storage.conn.execute(
            "PRAGMA table_info(session_summaries)"
        ) as cur:
            cols2 = {row[1] for row in await cur.fetchall()}
        assert "extracted_anchors" in cols2
    finally:
        await storage.close()


# ── Integration: anchor archive + exact lookup ───────────────────────────


@pytest.mark.asyncio
async def test_anchor_archive_and_lookup(tmp_path) -> None:
    """Archived entries with anchor_enabled get stable anchor IDs and are
    retrievable via exact anchor lookup."""
    storage = await _setup_storage(tmp_path)
    try:
        node = _node("agent:main:webchat:anchor-test", "sid-anchor")
        await storage.upsert_session(node)

        entries = [
            _entry("sid-anchor", "agent:main:webchat:anchor-test", "user", "first message", created_at=100),
            _entry("sid-anchor", "agent:main:webchat:anchor-test", "assistant", "second message", created_at=200),
            _entry("sid-anchor", "agent:main:webchat:anchor-test", "user", "third message", created_at=300),
        ]

        summary = SessionSummary(
            session_id="sid-anchor",
            session_key="agent:main:webchat:anchor-test",
            summary_text="Summary with [anchor:0:entry_001] reference",
        )

        await storage.rewrite_compacted_session(
            node=node,
            summary=summary,
            entries=[entries[2]],
            archived_entries=entries[:2],
            anchor_enabled=True,
            extracted_anchors=[{"compaction_index": 0, "entry_anchor_id": "entry_001"}],
        )

        # Exact anchor lookup — entry_000
        results = await storage.search_transcript(
            session_id="sid-anchor",
            anchor="0:entry_000",
        )
        assert len(results) == 1
        assert results[0]["snippet"] == "first message"
        assert results[0]["source"] == "archived"
        assert results[0]["anchor"] == "0:entry_000"

        # Exact anchor lookup — entry_001
        results2 = await storage.search_transcript(
            session_id="sid-anchor",
            anchor="0:entry_001",
        )
        assert len(results2) == 1
        assert results2[0]["snippet"] == "second message"

        # Non-existent anchor returns empty
        results3 = await storage.search_transcript(
            session_id="sid-anchor",
            anchor="0:entry_099",
        )
        assert results3 == []
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_anchor_ids_are_sequential(tmp_path) -> None:
    """Anchor IDs are assigned sequentially based on (created_at, id) order."""
    storage = await _setup_storage(tmp_path)
    try:
        node = _node("agent:main:webchat:seq", "sid-seq")
        await storage.upsert_session(node)

        # Deliberately out-of-order created_at
        entries = [
            _entry("sid-seq", "agent:main:webchat:seq", "assistant", "second", created_at=200),
            _entry("sid-seq", "agent:main:webchat:seq", "user", "first", created_at=100),
            _entry("sid-seq", "agent:main:webchat:seq", "user", "third", created_at=300),
        ]
        summary = SessionSummary(
            session_id="sid-seq",
            session_key="agent:main:webchat:seq",
            summary_text="summary",
        )
        await storage.rewrite_compacted_session(
            node=node,
            summary=summary,
            entries=[],
            archived_entries=entries,
            anchor_enabled=True,
        )

        async with storage.conn.execute(
            "SELECT compaction_anchor_id, content "
            "FROM compacted_transcript_entries "
            "WHERE session_id = ? ORDER BY created_at",
            ["sid-seq"],
        ) as cur:
            rows = await cur.fetchall()

        # Sorted by created_at: first(100) → second(200) → third(300)
        assert rows[0]["compaction_anchor_id"] == "entry_000"
        assert rows[0]["content"] == "first"
        assert rows[1]["compaction_anchor_id"] == "entry_001"
        assert rows[1]["content"] == "second"
        assert rows[2]["compaction_anchor_id"] == "entry_002"
        assert rows[2]["content"] == "third"
    finally:
        await storage.close()


# ── Integration: error handling ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_anchor_lookup_requires_session_id(tmp_path) -> None:
    storage = await _setup_storage(tmp_path)
    try:
        with pytest.raises(ValueError, match="requires session_id"):
            await storage.search_transcript(anchor="0:entry_000")
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_anchor_invalid_format(tmp_path) -> None:
    storage = await _setup_storage(tmp_path)
    try:
        with pytest.raises(ValueError, match="invalid anchor format"):
            await storage.search_transcript(session_id="sid", anchor="bad_format")
        with pytest.raises(ValueError, match="invalid compaction_index"):
            await storage.search_transcript(session_id="sid", anchor="abc:entry_000")
    finally:
        await storage.close()


# ── Integration: backward compatibility ──────────────────────────────────


@pytest.mark.asyncio
async def test_anchor_disabled_no_anchor_ids(tmp_path) -> None:
    """When anchor_enabled=False, no compaction_anchor_id is written."""
    storage = await _setup_storage(tmp_path)
    try:
        node = _node("agent:main:webchat:no-anchor", "sid-no-anchor")
        await storage.upsert_session(node)

        entries = [
            _entry("sid-no-anchor", "agent:main:webchat:no-anchor", "user", "hello", created_at=100),
        ]
        summary = SessionSummary(
            session_id="sid-no-anchor",
            session_key="agent:main:webchat:no-anchor",
            summary_text="plain summary",
        )
        await storage.rewrite_compacted_session(
            node=node,
            summary=summary,
            entries=[],
            archived_entries=entries,
            anchor_enabled=False,
        )

        # Anchor lookup returns empty (no anchor IDs written)
        results = await storage.search_transcript(
            session_id="sid-no-anchor",
            anchor="0:entry_000",
        )
        assert results == []

        # FTS still works
        fts_results = await storage.search_transcript("hello", session_id="sid-no-anchor")
        assert len(fts_results) >= 1
        assert fts_results[0]["source"] == "archived"
    finally:
        await storage.close()


# ── Integration: extracted_anchors persistence ───────────────────────────


@pytest.mark.asyncio
async def test_extracted_anchors_persisted_to_summary(tmp_path) -> None:
    """extracted_anchors are stored in session_summaries.extracted_anchors."""
    storage = await _setup_storage(tmp_path)
    try:
        node = _node("agent:main:webchat:persist", "sid-persist")
        await storage.upsert_session(node)

        entries = [
            _entry("sid-persist", "agent:main:webchat:persist", "user", "decision", created_at=100),
        ]
        anchors = [{"compaction_index": 0, "entry_anchor_id": "entry_000"}]
        summary = SessionSummary(
            session_id="sid-persist",
            session_key="agent:main:webchat:persist",
            summary_text="decision [anchor:0:entry_000]",
        )
        await storage.rewrite_compacted_session(
            node=node,
            summary=summary,
            entries=[],
            archived_entries=entries,
            anchor_enabled=True,
            extracted_anchors=anchors,
        )

        async with storage.conn.execute(
            "SELECT extracted_anchors FROM session_summaries WHERE session_id = ?",
            ["sid-persist"],
        ) as cur:
            row = await cur.fetchone()

        assert row is not None
        stored = json.loads(row["extracted_anchors"])
        assert len(stored) == 1
        assert stored[0]["entry_anchor_id"] == "entry_000"
    finally:
        await storage.close()


# ── Integration: compaction end-to-end with anchor_enabled ───────────────


@pytest.mark.asyncio
async def test_compaction_with_anchor_enabled(monkeypatch) -> None:
    """compact_context with anchor_enabled produces extracted_anchors."""
    calls: list[dict] = []

    async def fake_llm(**kwargs):
        calls.append(kwargs)
        return "Summary of discussion [anchor:0:entry_002] about the topic."

    monkeypatch.setattr(
        "opensquilla.session.compaction.call_compaction_llm", fake_llm
    )

    entries = [
        {"role": "user", "content": f"message {i} " + "x" * 50, "token_count": 100}
        for i in range(20)
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=500,
            config=CompactionConfig(
                api_key="sk-test",
                model="test-model",
                base_url="http://localhost/v1",
                anchor_enabled=True,
            ),
            compaction_index=0,
        )
    )

    assert result.summary_source == "llm"
    assert result.extracted_anchors is not None
    assert len(result.extracted_anchors) == 1
    assert result.extracted_anchors[0] == {
        "compaction_index": 0,
        "entry_anchor_id": "entry_002",
    }

    # Verify the LLM received anchor-labeled input
    assert len(calls) >= 1
    assert "[entry_000 |" in calls[0]["chunk_text"]
    assert calls[0]["compaction_index"] == 0


@pytest.mark.asyncio
async def test_compaction_without_anchor_no_extraction(monkeypatch) -> None:
    """compact_context without anchor_enabled returns extracted_anchors=None."""

    async def fake_llm(**kwargs):
        return "Plain summary without anchors."

    monkeypatch.setattr(
        "opensquilla.session.compaction.call_compaction_llm", fake_llm
    )

    entries = [
        {"role": "user", "content": f"message {i} " + "x" * 50, "token_count": 100}
        for i in range(20)
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=500,
            config=CompactionConfig(
                api_key="sk-test",
                model="test-model",
                base_url="http://localhost/v1",
                anchor_enabled=False,
            ),
        )
    )

    assert result.extracted_anchors is None
