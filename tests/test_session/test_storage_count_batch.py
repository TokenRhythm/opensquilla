"""Tests for SessionStorage count and bounded preview contracts.

Pins the transcript batch contract used by rpc_sessions.list and the exact
stored-session total used by the Overview KPI. Behaviour requirements:
- Empty list returns {}.
- Single id matches the legacy single-id path.
- Many ids (>500) chunk correctly and still return one entry per id.
- Sessions with no transcript entries are explicitly mapped to 0, not absent.
- Preview reads return only the newest eligible message and stay character-bounded.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from opensquilla.gateway.guest_rpc_policy import guest_owned_session_key
from opensquilla.session.models import SessionNode, TranscriptEntry
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


async def test_last_transcript_content_batch_returns_bounded_latest_message(
    storage: SessionStorage,
) -> None:
    await _seed_session(storage, "sid-preview", 0)
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_id="sid-preview",
            session_key="agent:test:sid-preview",
            message_id="system-old",
            role="system",
            content="system content should not be shown",
            created_at=1,
        )
    )
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_id="sid-preview",
            session_key="agent:test:sid-preview",
            message_id="user-old",
            role="user",
            content="older user message",
            created_at=2,
        )
    )
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_id="sid-preview",
            session_key="agent:test:sid-preview",
            message_id="assistant-new",
            role="assistant",
            content="newest assistant message " + ("x" * 200),
            created_at=3,
        )
    )
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_id="sid-preview",
            session_key="agent:test:sid-preview",
            message_id="empty-latest",
            role="user",
            content="",
            created_at=4,
        )
    )

    result = await storage.list_last_transcript_content_batch(
        ["sid-preview", "missing"],
        max_chars=10,
    )

    assert result == {
        "sid-preview": "newest ass",
        "missing": "",
    }


async def test_last_transcript_content_batch_uses_id_as_tie_breaker(
    storage: SessionStorage,
) -> None:
    await _seed_session(storage, "sid-tie", 0)
    for message_id, content in (("tie-a", "first"), ("tie-b", "second")):
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id="sid-tie",
                session_key="agent:test:sid-tie",
                message_id=message_id,
                role="user",
                content=content,
                created_at=10,
            )
        )

    result = await storage.list_last_transcript_content_batch(["sid-tie"])

    assert result["sid-tie"] == "second"


async def test_last_transcript_content_batch_chunks_large_session_lists(
    storage: SessionStorage,
) -> None:
    session_ids = [f"sid-{index}" for index in range(301)]
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_id=session_ids[0],
            session_key="agent:test:sid-0",
            role="assistant",
            content="first chunk",
            created_at=1,
        )
    )
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_id=session_ids[-1],
            session_key=f"agent:test:{session_ids[-1]}",
            role="user",
            content="second chunk",
            created_at=1,
        )
    )

    result = await storage.list_last_transcript_content_batch(session_ids)

    assert len(result) == len(session_ids)
    assert result[session_ids[0]] == "first chunk"
    assert result[session_ids[-1]] == "second chunk"
    assert result[session_ids[1]] == ""


async def test_session_count_can_scope_to_one_guest_owner(
    storage: SessionStorage,
) -> None:
    owner_id = "a" * 64
    other_owner_id = "b" * 64
    sessions = (
        (guest_owned_session_key(owner_id, "one"), "guest-one"),
        (guest_owned_session_key(owner_id, "two"), "guest-two"),
        (guest_owned_session_key(other_owner_id, "other"), "guest-other"),
        ("agent:main:webchat:host", "host"),
    )
    for session_key, session_id in sessions:
        await storage.upsert_session(
            SessionNode(
                session_key=session_key,
                session_id=session_id,
                agent_id="main",
                status="idle",
                created_at=1,
                updated_at=1,
            )
        )

    assert await storage.count_sessions() == 4
    assert await storage.count_sessions(guest_owner_id=owner_id) == 2
    assert await storage.count_sessions(guest_owner_id="invalid") == 0
