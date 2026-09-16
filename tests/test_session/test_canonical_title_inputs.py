"""Early title input must survive compaction without loading complete histories."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[SessionStorage]:
    store = SessionStorage(str(tmp_path / "canonical-title-inputs.db"))
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


async def _seed(
    storage: SessionStorage,
    name: str,
    messages: list[tuple[str, str | None]],
    *,
    same_timestamp: bool = False,
) -> tuple[SessionManager, SessionNode]:
    manager = SessionManager(storage, inject_time_prefix=False)
    node = await manager.create(f"agent:main:webchat:{name}")
    for index, (role, content) in enumerate(messages):
        await storage.append_transcript_entry(
            TranscriptEntry(
                session_id=node.session_id,
                session_key=node.session_key,
                message_id=f"{name}-{index}",
                role=role,
                content=content,
                created_at=1_000 if same_timestamp else 1_000 + index,
            )
        )
    return manager, node


async def test_early_inputs_survive_repeated_real_compaction_and_reopen(
    storage: SessionStorage,
) -> None:
    contents = [f"Sample topic {index}" for index in range(8)]
    manager, node = await _seed(storage, "repeated", [("user", text) for text in contents])
    for number, kept_start in enumerate((2, 5)):
        assert await manager.persist_compaction_result(
            node.session_key,
            f"Synthetic summary {number}",
            [{"role": "user", "content": text} for text in contents[kept_start:]],
            compaction_id=f"sample-compaction-{number}",
        )
        assert await storage.list_canonical_user_transcript_content_batch([node.session_id]) == {
            node.session_id: contents[:3],
        }

    # The old active-only projection deliberately retains its existing semantics.
    assert await storage.list_user_transcript_content_batch([node.session_id]) == {
        node.session_id: contents[5:],
    }
    await storage.close()
    await storage.connect()
    assert await storage.list_canonical_user_transcript_content_batch([node.session_id]) == {
        node.session_id: contents[:3],
    }


async def test_same_timestamp_uses_original_ids_not_archive_insertion_ids(
    storage: SessionStorage,
) -> None:
    contents = [f"Ordered sample {index}" for index in range(5)]
    manager, node = await _seed(
        storage,
        "same-time",
        [("user", text) for text in contents],
        same_timestamp=True,
    )
    assert await manager.persist_compaction_result(
        node.session_key,
        "Synthetic summary",
        [{"role": "user", "content": contents[-1]}],
        compaction_id="same-time-compaction",
    )
    # Archive row identities are independent of original transcript identities.
    await storage.conn.execute(
        "UPDATE compacted_transcript_entries SET id = -id WHERE session_id = ?",
        (node.session_id,),
    )
    await storage.conn.commit()

    canonical = await manager.get_canonical_transcript(node.session_key)
    assert [entry.content for entry in canonical] == contents
    assert await storage.list_canonical_user_transcript_content_batch(
        [node.session_id], limit_per_session=5,
    ) == {node.session_id: contents}


async def test_batch_handles_archive_only_active_only_mixed_and_missing_sessions(
    storage: SessionStorage,
) -> None:
    nodes = {}
    for name, kept in (("archive", 0), ("active", 3), ("mixed", 1)):
        contents = [f"{name} sample {index}" for index in range(3)]
        manager, node = await _seed(storage, name, [("user", text) for text in contents])
        nodes[name] = node
        if kept < 3:
            tail = contents[-kept:] if kept else []
            assert await manager.persist_compaction_result(
                node.session_key,
                "Synthetic summary",
                [{"role": "user", "content": text} for text in tail],
                compaction_id=f"{name}-compaction",
            )
    _, empty = await _seed(storage, "empty", [])
    selected = [node.session_id for node in nodes.values()] + [empty.session_id, "missing"]
    queries: list[str] = []
    before_changes = storage.conn.total_changes
    await storage.conn.set_trace_callback(queries.append)
    try:
        actual = await storage.list_canonical_user_transcript_content_batch(selected)
    finally:
        await storage.conn.set_trace_callback(None)

    assert actual == {
        **{node.session_id: [f"{name} sample {i}" for i in range(3)]
           for name, node in nodes.items()},
        empty.session_id: [],
        "missing": [],
    }
    assert storage.conn.total_changes == before_changes
    assert len(queries) == 1
    assert "transcript_entries" in queries[0]
    assert "compacted_transcript_entries" in queries[0]


async def test_only_nonempty_user_content_is_selected_without_deduplication(
    storage: SessionStorage,
) -> None:
    messages = [
        ("system", "System instructions"),
        ("user", None),
        ("user", ""),
        ("assistant", "Assistant reply"),
        ("user", "Repeated sample"),
        ("user", "Repeated sample"),
        ("user", "Later sample"),
    ]
    manager, node = await _seed(storage, "filtered", messages)
    assert await manager.persist_compaction_result(
        node.session_key,
        "Synthetic summary",
        [{"role": role, "content": content} for role, content in messages[5:]],
        compaction_id="filtered-compaction",
    )
    assert await storage.list_canonical_user_transcript_content_batch(
        [node.session_id, node.session_id], limit_per_session=2,
    ) == {node.session_id: ["Repeated sample", "Repeated sample"]}


async def test_large_batch_chunks_reads_within_sqlite_variable_limit(
    storage: SessionStorage,
) -> None:
    _, node = await _seed(storage, "chunked", [("user", "Sample first message")])
    session_ids = [f"missing-{index}" for index in range(650)]
    session_ids.insert(301, node.session_id)
    queries: list[str] = []
    await storage.conn.set_trace_callback(queries.append)
    try:
        result = await storage.list_canonical_user_transcript_content_batch(session_ids)
    finally:
        await storage.conn.set_trace_callback(None)

    assert result == {
        sid: ["Sample first message"] if sid == node.session_id else []
        for sid in session_ids
    }
    assert len(queries) == 3


@pytest.mark.parametrize("session_ids,limit", [([], 3), (["missing"], 0), (["missing"], -1)])
async def test_empty_request_needs_no_read(
    storage: SessionStorage, session_ids: list[str], limit: int,
) -> None:
    queries: list[str] = []
    await storage.conn.set_trace_callback(queries.append)
    try:
        actual = await storage.list_canonical_user_transcript_content_batch(
            session_ids, limit_per_session=limit,
        )
    finally:
        await storage.conn.set_trace_callback(None)
    assert actual == {sid: [] for sid in session_ids}
    assert queries == []
