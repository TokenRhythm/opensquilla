"""Checkpoint-only replacements preserve, but cannot invent, raw archive proof."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from opensquilla.session.compaction_state import StructuredCompactionSummary
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage


@pytest_asyncio.fixture
async def manager():
    storage = SessionStorage(":memory:")
    await storage.connect()
    yield SessionManager(storage, inject_time_prefix=False)
    await storage.close()


def _payload(text: str, *, replaces: bool) -> dict:
    return StructuredCompactionSummary(
        current_status=text, source_coverage={"replaces_prior_context": replaces},
    ).model_dump(mode="json")


async def _seed_replacements(manager):
    node = await manager.create("agent:main:checkpoint-coverage")
    for role, text in [("user", "original goal"), ("assistant", "old response"),
                       ("user", "current request")]:
        await manager.append_message(node.session_key, role, text)
    raw = await manager.get_transcript(node.session_key)
    source = await manager.capture_compaction_source(node.session_key)
    result = await manager.persist_compaction_result(
        node.session_key, "S0", [], summary_payload=_payload("S0", replaces=False),
        summary_format="structured_v1", source_snapshot=source, removed_count=2,
    )
    assert result.applied
    assert (await manager._storage.get_canonical_transcript_coverage(
        node.session_id,
    )).canonical_complete
    for text in ("S1", "S2"):
        source = await manager.capture_compaction_source(node.session_key)
        result = await manager.persist_compaction_result(
            node.session_key, text, [], summary_payload=_payload(text, replaces=True),
            summary_format="structured_v1", source_snapshot=source, removed_count=0,
        )
        assert result.applied
    return node, raw


@pytest.mark.asyncio
async def test_checkpoint_only_chain_keeps_complete_canonical_archive_and_fork(manager):
    node, raw = await _seed_replacements(manager)
    coverage = await manager._storage.get_canonical_transcript_coverage(node.session_id)
    assert coverage.canonical_complete
    assert coverage.compaction_count == 3
    assert [entry.model_dump() for entry in await manager.get_canonical_transcript(
        node.session_key,
    )] == [entry.model_dump() for entry in raw]
    summaries = await manager.get_summaries(node.session_key)
    assert [summary.removed_count for summary in summaries] == [2, 0, 0]
    assert len({summary.covered_through_id for summary in summaries}) == 1
    child = await manager.branch(
        node.session_key, "agent:main:checkpoint-coverage-child", fork_transcript=True,
    )
    assert (await manager._storage.get_canonical_transcript_coverage(
        child.session_id,
    )).canonical_complete
    assert [entry.content for entry in await manager.get_canonical_transcript(
        child.session_key,
    )] == [entry.content for entry in raw]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "missing_payload", "malformed_json", "missing_flag", "false_flag", "numeric_flag",
    "text_format", "unproven_boundary", "missing_archive", "missing_compaction_id",
    "missing_original_id", "count_mismatch",
])
async def test_checkpoint_only_metadata_cannot_hide_incomplete_archive(manager, mutation):
    node, _raw = await _seed_replacements(manager)
    summaries = await manager.get_summaries(node.session_key)
    latest = summaries[-1]
    conn = manager._storage.conn
    if mutation in {"missing_payload", "malformed_json", "missing_flag", "false_flag",
                    "numeric_flag"}:
        encoded = {
            "missing_payload": None,
            "malformed_json": "{broken-json",
            "missing_flag": json.dumps({"current_status": "S2"}),
            "false_flag": json.dumps(_payload("S2", replaces=False)),
            "numeric_flag": json.dumps({"source_coverage": {"replaces_prior_context": 1}}),
        }[mutation]
        await conn.execute("UPDATE session_summaries SET summary_payload = ? WHERE id = ?",
                           (encoded, latest.id))
    elif mutation == "text_format":
        await conn.execute("UPDATE session_summaries SET summary_format = 'text' WHERE id = ?",
                           (latest.id,))
    elif mutation == "unproven_boundary":
        await conn.execute("UPDATE session_summaries SET covered_through_id = ? WHERE id = ?",
                           (latest.covered_through_id + 100, latest.id))
    elif mutation == "missing_archive":
        await conn.execute("DELETE FROM compacted_transcript_entries WHERE session_id = ?",
                           (node.session_id,))
    elif mutation == "missing_compaction_id":
        await conn.execute("UPDATE session_summaries SET compaction_id = NULL WHERE id = ?",
                           (latest.id,))
    elif mutation == "missing_original_id":
        await conn.execute(
            "UPDATE compacted_transcript_entries SET original_entry_id = NULL WHERE session_id = ?",
            (node.session_id,),
        )
    else:
        await conn.execute("UPDATE sessions SET compaction_count = 4 WHERE session_id = ?",
                           (node.session_id,))
    await conn.commit()
    assert not (await manager._storage.get_canonical_transcript_coverage(
        node.session_id,
    )).canonical_complete
