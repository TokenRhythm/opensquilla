"""Tests for L0: archived (compacted) transcript FTS search.

Verifies that entries moved to ``compacted_transcript_entries`` during
compaction remain full-text searchable via ``search_transcript()`` and
``search_transcript_like()``, with correct ``source`` provenance tagging.
"""

from __future__ import annotations

import pytest

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


def _summary(session_id: str, session_key: str, text: str) -> SessionSummary:
    return SessionSummary(
        session_id=session_id,
        session_key=session_key,
        summary_text=text,
    )


async def _setup_storage(tmp_path) -> SessionStorage:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    return storage


async def _create_session_with_entries(
    storage: SessionStorage,
    session_key: str,
    session_id: str,
    entries: list[tuple[str, str]],  # (role, content)
) -> SessionNode:
    """Create a session, upsert it, and append transcript entries."""
    node = _node(session_key, session_id)
    await storage.upsert_session(node)
    for role, content in entries:
        await storage.append_transcript_entry(
            _entry(session_id, session_key, role, content)
        )
    return node


async def _compact_session(
    storage: SessionStorage,
    node: SessionNode,
    *,
    archived_contents: list[tuple[str, str]],
    kept_contents: list[tuple[str, str]],
) -> None:
    """Simulate compaction: archive some entries, keep others."""
    archived = [
        _entry(node.session_id, node.session_key, role, content)
        for role, content in archived_contents
    ]
    kept = [
        _entry(node.session_id, node.session_key, role, content)
        for role, content in kept_contents
    ]
    summary = _summary(node.session_id, node.session_key, "compaction summary")
    await storage.rewrite_compacted_session(
        node=node,
        summary=summary,
        entries=kept,
        archived_entries=archived,
    )


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_archived_entry_searchable_after_compaction(tmp_path) -> None:
    """A term only present in the archived prefix is found with source='archived'."""
    storage = await _setup_storage(tmp_path)
    try:
        node = await _create_session_with_entries(
            storage,
            "agent:main:webchat:arch-test",
            "sid-arch",
            [("user", "the quantum flux capacitor is ready")],
        )
        await _compact_session(
            storage,
            node,
            archived_contents=[("user", "the quantum flux capacitor is ready")],
            kept_contents=[("assistant", "acknowledged")],
        )

        results = await storage.search_transcript("quantum flux capacitor")
        assert len(results) >= 1
        hit = results[0]
        assert hit["source"] == "archived"
        assert "quantum" in hit["snippet"].lower()
        assert hit["session_key"] == "agent:main:webchat:arch-test"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_active_entry_still_searchable(tmp_path) -> None:
    """Active (non-compacted) entries retain source='active'."""
    storage = await _setup_storage(tmp_path)
    try:
        await _create_session_with_entries(
            storage,
            "agent:main:webchat:active-test",
            "sid-active",
            [("assistant", "deploying the neural mesh network")],
        )

        results = await storage.search_transcript("neural mesh network")
        assert len(results) >= 1
        hit = results[0]
        assert hit["source"] == "active"
        assert "neural" in hit["snippet"].lower()
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_session_id_filter_archived(tmp_path) -> None:
    """session_id filter restricts archived results to the correct session."""
    storage = await _setup_storage(tmp_path)
    try:
        # Session A: archived entry with unique term
        node_a = await _create_session_with_entries(
            storage,
            "agent:main:webchat:session-a",
            "sid-a",
            [("user", "alpha protocol initiated")],
        )
        await _compact_session(
            storage,
            node_a,
            archived_contents=[("user", "alpha protocol initiated")],
            kept_contents=[("assistant", "ok")],
        )

        # Session B: archived entry with same term
        node_b = await _create_session_with_entries(
            storage,
            "agent:main:webchat:session-b",
            "sid-b",
            [("user", "alpha protocol terminated")],
        )
        await _compact_session(
            storage,
            node_b,
            archived_contents=[("user", "alpha protocol terminated")],
            kept_contents=[("assistant", "ok")],
        )

        # Search restricted to session A
        results = await storage.search_transcript(
            "alpha protocol", session_id="sid-a"
        )
        assert len(results) >= 1
        for r in results:
            assert r["session_key"] == "agent:main:webchat:session-a"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_include_archived_false(tmp_path) -> None:
    """include_archived=False suppresses archived hits."""
    storage = await _setup_storage(tmp_path)
    try:
        node = await _create_session_with_entries(
            storage,
            "agent:main:webchat:no-arch",
            "sid-noarch",
            [("user", "the chroniton field is destabilizing")],
        )
        await _compact_session(
            storage,
            node,
            archived_contents=[("user", "the chroniton field is destabilizing")],
            kept_contents=[("assistant", "noted")],
        )

        results = await storage.search_transcript(
            "chroniton field", include_archived=False
        )
        assert len(results) == 0
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_fts_trigger_insert(tmp_path) -> None:
    """Manually inserting into compacted_transcript_entries makes it searchable."""
    storage = await _setup_storage(tmp_path)
    try:
        node = _node("agent:main:webchat:trigger-ins", "sid-trig-ins")
        await storage.upsert_session(node)

        # Direct insert into archive table (bypasses rewrite_compacted_session)
        await storage.conn.execute(
            "INSERT INTO compacted_transcript_entries "
            "(session_id, session_key, message_id, role, content, created_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sid-trig-ins", "agent:main:webchat:trigger-ins", "msg-1",
             "user", "tachyon pulse detected", 1000, 2000),
        )
        await storage.conn.commit()

        results = await storage.search_transcript("tachyon pulse")
        assert len(results) >= 1
        assert results[0]["source"] == "archived"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_fts_trigger_delete(tmp_path) -> None:
    """Deleting an archived row removes it from FTS search."""
    storage = await _setup_storage(tmp_path)
    try:
        node = _node("agent:main:webchat:trigger-del", "sid-trig-del")
        await storage.upsert_session(node)

        await storage.conn.execute(
            "INSERT INTO compacted_transcript_entries "
            "(session_id, session_key, message_id, role, content, created_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sid-trig-del", "agent:main:webchat:trigger-del", "msg-del",
             "user", "antimatter containment breach", 1000, 2000),
        )
        await storage.conn.commit()

        # Verify searchable
        results = await storage.search_transcript("antimatter containment")
        assert len(results) >= 1

        # Delete the row
        await storage.conn.execute(
            "DELETE FROM compacted_transcript_entries WHERE message_id = 'msg-del'"
        )
        await storage.conn.commit()

        # Verify no longer searchable
        results = await storage.search_transcript("antimatter containment")
        assert len(results) == 0
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_fts_trigger_update(tmp_path) -> None:
    """Updating content of an archived row updates FTS index."""
    storage = await _setup_storage(tmp_path)
    try:
        node = _node("agent:main:webchat:trigger-upd", "sid-trig-upd")
        await storage.upsert_session(node)

        await storage.conn.execute(
            "INSERT INTO compacted_transcript_entries "
            "(session_id, session_key, message_id, role, content, created_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sid-trig-upd", "agent:main:webchat:trigger-upd", "msg-upd",
             "user", "plasma conduit nominal", 1000, 2000),
        )
        await storage.conn.commit()

        # Old content searchable
        results = await storage.search_transcript("plasma conduit")
        assert len(results) >= 1

        # Update content
        await storage.conn.execute(
            "UPDATE compacted_transcript_entries SET content = ? WHERE message_id = ?",
            ("graviton emitter recalibrated", "msg-upd"),
        )
        await storage.conn.commit()

        # Old content no longer searchable
        results = await storage.search_transcript("plasma conduit")
        assert len(results) == 0

        # New content searchable
        results = await storage.search_transcript("graviton emitter")
        assert len(results) >= 1
        assert results[0]["source"] == "archived"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_null_content_skipped(tmp_path) -> None:
    """Rows with NULL content do not cause FTS errors and are not searchable."""
    storage = await _setup_storage(tmp_path)
    try:
        node = _node("agent:main:webchat:null-content", "sid-null")
        await storage.upsert_session(node)

        await storage.conn.execute(
            "INSERT INTO compacted_transcript_entries "
            "(session_id, session_key, message_id, role, content, created_at, archived_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sid-null", "agent:main:webchat:null-content", "msg-null",
             "tool", None, 1000, 2000),
        )
        await storage.conn.commit()

        # Should not raise; no archived results for any query
        results = await storage.search_transcript("anything")
        assert all(
            r.get("source") != "archived" or r["session_key"] != "agent:main:webchat:null-content"
            for r in results
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_migration_idempotent(tmp_path) -> None:
    """Running _create_schema() twice does not error or duplicate FTS rows."""
    db_path = str(tmp_path / "sessions.db")

    # First connection creates schema
    storage1 = SessionStorage(db_path)
    await storage1.connect()
    node = _node("agent:main:webchat:idem", "sid-idem")
    await storage1.upsert_session(node)
    await storage1.conn.execute(
        "INSERT INTO compacted_transcript_entries "
        "(session_id, session_key, message_id, role, content, created_at, archived_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("sid-idem", "agent:main:webchat:idem", "msg-idem",
         "user", "warp core breaching", 1000, 2000),
    )
    await storage1.conn.commit()
    await storage1.close()

    # Second connection re-runs _create_schema() (idempotent)
    storage2 = SessionStorage(db_path)
    await storage2.connect()
    try:
        results = await storage2.search_transcript("warp core")
        assert len(results) >= 1
        assert results[0]["source"] == "archived"

        # Verify no duplicate FTS rows
        async with storage2.conn.execute(
            "SELECT COUNT(*) FROM compacted_transcript_fts"
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == 1  # exactly one FTS row, not duplicated
    finally:
        await storage2.close()


@pytest.mark.asyncio
async def test_backfill_populates_existing(tmp_path) -> None:
    """Pre-existing archive rows (without FTS) are backfilled on migration."""
    db_path = str(tmp_path / "sessions.db")

    # Create a database with archive rows but manually drop the FTS table
    # to simulate a pre-L0 database.
    storage = SessionStorage(db_path)
    await storage.connect()

    node = _node("agent:main:webchat:backfill", "sid-bf")
    await storage.upsert_session(node)

    # Insert archive row (trigger will add to FTS since it exists)
    await storage.conn.execute(
        "INSERT INTO compacted_transcript_entries "
        "(session_id, session_key, message_id, role, content, created_at, archived_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("sid-bf", "agent:main:webchat:backfill", "msg-bf",
         "user", "dilithium crystal matrix degrading", 1000, 2000),
    )
    await storage.conn.commit()

    # Drop FTS table and triggers to simulate pre-L0 state
    await storage.conn.execute("DROP TRIGGER IF EXISTS compacted_transcript_fts_ai")
    await storage.conn.execute("DROP TRIGGER IF EXISTS compacted_transcript_fts_ad")
    await storage.conn.execute("DROP TRIGGER IF EXISTS compacted_transcript_fts_au")
    await storage.conn.execute("DROP TABLE IF EXISTS compacted_transcript_fts")
    await storage.conn.commit()
    await storage.close()

    # Reconnect: migration should recreate FTS and backfill
    storage2 = SessionStorage(db_path)
    await storage2.connect()
    try:
        results = await storage2.search_transcript("dilithium crystal")
        assert len(results) >= 1
        assert results[0]["source"] == "archived"
        assert results[0]["session_key"] == "agent:main:webchat:backfill"
    finally:
        await storage2.close()


@pytest.mark.asyncio
async def test_like_search_includes_archived(tmp_path) -> None:
    """search_transcript_like also returns archived results."""
    storage = await _setup_storage(tmp_path)
    try:
        node = await _create_session_with_entries(
            storage,
            "agent:main:webchat:like-arch",
            "sid-like",
            [("user", "subspace interference pattern detected")],
        )
        await _compact_session(
            storage,
            node,
            archived_contents=[("user", "subspace interference pattern detected")],
            kept_contents=[("assistant", "copy")],
        )

        results = await storage.search_transcript_like("subspace interference")
        assert len(results) >= 1
        hit = results[0]
        assert hit["source"] == "archived"
        assert "subspace" in hit["snippet"].lower()
    finally:
        await storage.close()
