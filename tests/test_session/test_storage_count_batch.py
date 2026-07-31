"""Tests for SessionStorage.count_transcript_entries_batch.

Pins the batch counting contract used by rpc_sessions.list to avoid the
per-row N+1 against count_transcript_entries. Behaviour requirements:
- Empty list returns {}.
- Single id matches the legacy single-id path.
- Many ids (>500) chunk correctly and still return one entry per id.
- Sessions with no transcript entries are explicitly mapped to 0, not absent.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from opensquilla.session.storage import SessionStorage


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.db"
        store = SessionStorage(str(path))
        await store.connect()
        try:
            yield store
        finally:
            await store.close()


async def _seed_session(storage: SessionStorage, session_id: str, entry_count: int) -> None:
    """Create a session row and append `entry_count` transcript entries."""
    from opensquilla.session.models import SessionNode, TranscriptEntry

    node = SessionNode(
        session_key=f"agent:test:{session_id}",
        session_id=session_id,
        agent_id="test",
        status="idle",
        created_at=1,
        updated_at=1,
    )
    await storage.upsert_session(node)
    for i in range(entry_count):
        entry = TranscriptEntry(
            session_id=session_id,
            message_id=f"{session_id}-msg-{i}",
            role="user" if i % 2 == 0 else "assistant",
            content=f"entry {i}",
            created_at=i + 1,
        )
        await storage.append_transcript_entry(entry)


async def test_batch_count_empty_list_returns_empty_dict(storage: SessionStorage) -> None:
    assert await storage.count_transcript_entries_batch([]) == {}


async def test_batch_count_matches_single_id_path(storage: SessionStorage) -> None:
    await _seed_session(storage, "sid-a", 3)
    await _seed_session(storage, "sid-b", 0)
    await _seed_session(storage, "sid-c", 7)

    legacy = {
        sid: await storage.count_transcript_entries(sid)
        for sid in ("sid-a", "sid-b", "sid-c")
    }
    batch = await storage.count_transcript_entries_batch(["sid-a", "sid-b", "sid-c"])
    assert batch == legacy
    # Explicit 0 for empty-transcript session, not absent.
    assert batch["sid-b"] == 0


async def test_batch_count_missing_session_returns_zero(storage: SessionStorage) -> None:
    await _seed_session(storage, "real-sid", 2)
    result = await storage.count_transcript_entries_batch(["real-sid", "ghost-sid"])
    assert result == {"real-sid": 2, "ghost-sid": 0}


async def test_batch_count_chunks_above_500(storage: SessionStorage) -> None:
    # Seed 12 sessions with varied entry counts to keep the test fast, then
    # query with a 1500-id list (12 real + 1488 ghosts) to force >2 chunks
    # through the IN(?...) GROUP BY query. SQLITE_MAX_VARIABLE_NUMBER default
    # is 999; chunk size is 500.
    counts = {f"sid-{i}": (i % 5) for i in range(12)}
    for sid, n in counts.items():
        await _seed_session(storage, sid, n)

    all_ids = list(counts.keys()) + [f"ghost-{i}" for i in range(1500 - len(counts))]
    result = await storage.count_transcript_entries_batch(all_ids)

    assert len(result) == len(all_ids)
    for sid, expected in counts.items():
        assert result[sid] == expected, sid
    for sid in all_ids:
        if sid not in counts:
            assert result[sid] == 0


async def test_list_session_ids_updated_since_defaults_to_unlimited(
    storage: SessionStorage,
) -> None:
    """Profile selection must not silently truncate its history window."""

    from opensquilla.session.models import SessionNode

    for i in range(2005):
        await storage.upsert_session(
            SessionNode(
                session_key=f"agent:test:sid-{i}",
                session_id=f"sid-{i}",
                agent_id="test",
                status="idle",
                created_at=i + 1,
                updated_at=i + 1,
            )
        )

    rows = await storage.list_session_ids_updated_since(0, agent_id="test")
    limited = await storage.list_session_ids_updated_since(
        0,
        agent_id="test",
        limit=2000,
    )

    assert len(rows) == 2005
    assert rows[0] == ("sid-0", 1)
    assert rows[-1] == ("sid-2004", 2005)
    assert len(limited) == 2000
