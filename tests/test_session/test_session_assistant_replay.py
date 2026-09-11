"""Durable replay data follows the existing atomic transcript lifecycle."""

from copy import deepcopy

import pytest

from opensquilla.engine.history import decode_assistant_replay
from opensquilla.provider.types import Message, ProviderReplayState
from opensquilla.session.compaction import (
    estimate_entries_model_replay_chars,
    estimate_entry_model_replay_tokens,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.session.storage import _deserialize_row as deserialize_row


def _replay() -> dict:
    messages = [
        Message(
            role="assistant",
            content="accepted answer",
            reasoning_content="reasoning per call",
            provider_replay=ProviderReplayState(
                protocol="openai_chat_completions",
                source="synthetic-endpoint",
                model="dummy-model",
                reasoning_details=[{"type": "reasoning.encrypted", "data": "dummy-signature"}],
            ),
        )
    ]
    return {"version": 1, "messages": [m.model_dump(mode="json") for m in messages]}


@pytest.mark.asyncio
async def test_replay_round_trip_restart_archive_and_both_fork_paths(tmp_path):
    path = tmp_path / "sessions.db"
    storage = SessionStorage(str(path))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        node = await manager.create("agent:main:replay")
        await manager.append_message(node.session_key, "assistant", "legacy answer")
        envelope = _replay()
        recorded = await manager.append_message(
            node.session_key,
            "assistant",
            "display answer",
            assistant_replay=envelope,
        )
        envelope["messages"][0]["provider_replay"]["reasoning_details"].clear()
        assert recorded.assistant_replay == _replay()
        public_entries = await manager.read_transcript(node.session_key)
        assert all("assistant_replay" not in entry for entry in public_entries)
        latest = await manager.append_message(node.session_key, "user", "next question")
        await manager.persist_compaction_result(
            node.session_key,
            "older facts",
            [{"role": "user", "content": "next question"}],
            compaction_id="synthetic-compaction",
        )
    finally:
        await storage.close()

    storage = SessionStorage(str(path))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        canonical = await manager.get_canonical_transcript(node.session_key)
        assert canonical[0].assistant_replay is None
        assert canonical[1].assistant_replay == _replay()
        one = await storage.get_canonical_transcript_entry(node.session_id, recorded.message_id)
        assert one is not None and one.assistant_replay == _replay()
        assert (
            decode_assistant_replay(canonical[1].assistant_replay)[0].content == "accepted answer"
        )
        page = await manager.get_canonical_transcript_page(node.session_key, limit=10)
        assert page.entries[1].assistant_replay == _replay()
        archived = await storage.get_compacted_transcript_entries(
            session_id=node.session_id,
            compaction_id="synthetic-compaction",
        )
        assert archived[1].assistant_replay == _replay()
        child = await manager.branch(
            node.session_key,
            "agent:main:replay-child",
            fork_transcript=True,
            fork_before_message_id=latest.message_id,
        )
        forked = await manager.get_transcript(child.session_key)
        assert [entry.assistant_replay for entry in forked] == [None, _replay()]
        prepared = await manager.prepare_prefix_branch(
            node.session_key,
            "agent:main:replay-prepared",
            fork_before_message_id=latest.message_id,
        )
        assert [entry.assistant_replay for entry in prepared.initial_transcript_entries] == [
            None,
            _replay(),
        ]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_replay_survives_retained_tail_and_message_checkpoint_upsert():
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        node = await manager.create("agent:main:replay-upsert")
        await manager.append_message(node.session_key, "user", "older prompt")
        await manager.append_message(node.session_key, "assistant", "older answer")
        first = await manager.append_message(
            node.session_key,
            "assistant",
            "checkpoint",
            assistant_message_id="accepted-turn",
            assistant_replay=_replay(),
        )
        final = _replay()
        final["messages"].append(Message(role="assistant", content="final answer").model_dump())
        await manager.append_message(
            node.session_key,
            "assistant",
            "final display",
            assistant_message_id="accepted-turn",
            assistant_replay=final,
        )
        before = await manager.get_transcript(node.session_key)
        assert len(before) == 3
        assert before[-1].message_id == first.message_id
        source = await manager.capture_compaction_source(node.session_key)
        await manager.persist_compaction_result(
            node.session_key,
            "older facts",
            [{"role": "assistant", "content": "flattened"}],
            compaction_id="synthetic-tail",
        )
        assert (await manager.get_transcript(node.session_key))[0].assistant_replay == final
        assert source.entries[-1].assistant_replay == final
    finally:
        await storage.close()


def test_native_replay_budget_counts_state_once_and_ignores_display_aggregate():
    envelope = _replay()
    entry = {"role": "assistant", "assistant_replay": envelope}
    duplicate_display = {
        **entry,
        "content": "display " * 10000,
        "reasoning_content": "aggregate " * 10000,
        "token_count": 100000,
        "tool_calls": [{"text": "flattened " * 10000}],
    }
    assert estimate_entry_model_replay_tokens(entry) == estimate_entry_model_replay_tokens(
        duplicate_display
    )
    assert estimate_entries_model_replay_chars([entry]) == estimate_entries_model_replay_chars(
        [duplicate_display]
    )
    larger = deepcopy(entry)
    larger["assistant_replay"]["messages"][0]["provider_replay"]["reasoning_details"][0]["data"] = (
        "opaque " * 10000
    )
    assert estimate_entry_model_replay_tokens(larger) > estimate_entry_model_replay_tokens(entry)
    assert estimate_entries_model_replay_chars([larger]) > estimate_entries_model_replay_chars(
        [entry]
    )


@pytest.mark.parametrize("encoded", ["{private-dummy", "null", "[]", '"private-dummy"'])
def test_corrupt_replay_json_cannot_be_read_as_a_legacy_row(encoded):
    with pytest.raises(ValueError, match="assistant replay") as exc:
        deserialize_row({"assistant_replay": encoded})
    assert "private-dummy" not in str(exc.value)


@pytest.mark.asyncio
async def test_compaction_rejects_replay_only_source_change():
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        node = await manager.create("agent:main:replay-cas")
        await manager.append_message(
            node.session_key,
            "assistant",
            "unchanged display",
            assistant_message_id="old-turn",
            assistant_replay=_replay(),
        )
        active = await manager.append_message(node.session_key, "user", "active request")
        source = await manager.capture_compaction_source(
            node.session_key,
            boundary_message_id=active.message_id,
        )
        changed = _replay()
        changed["messages"][0]["provider_replay"]["reasoning_details"][0]["data"] = "new-dummy"
        await manager.append_message(
            node.session_key,
            "assistant",
            "unchanged display",
            assistant_message_id="old-turn",
            assistant_replay=changed,
        )
        installed = await manager.persist_compaction_result(
            node.session_key,
            "stale facts",
            [{"role": "user", "content": "active request"}],
            compaction_id="synthetic-cas",
            removed_count=1,
            source_entries=source.entries,
            source_preimage=source.preimage,
            source_boundary_message_id=source.boundary_message_id,
            source_boundary_entry_id=source.boundary_entry_id,
        )
        assert installed is False
        assert (await manager.get_transcript(node.session_key))[0].assistant_replay == changed
        assert await manager.get_summaries(node.session_key) == []
    finally:
        await storage.close()
