"""Idle cache reclamation must never stand in for execution-state cleanup."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from opensquilla.contracts.gateway_transport import TURN_COMMITTED_EVENT
from opensquilla.gateway.replay_budget import ReplayCacheBudget, retained_bytes
from opensquilla.gateway.session_streams import SessionStreamRegistry


def finish(registry: SessionStreamRegistry, key: str, task: str, text: str = "x") -> None:
    registry.record(key, "session.event.text_delta", {"task_id": task, "text": text})
    registry.record(key, "session.event.done", {"task_id": task})
    registry.mark_terminal_persisted(key, task, reconstructible=True)


def test_shared_and_cyclic_payloads_are_counted_once() -> None:
    shared = {"text": "x" * 1000}
    shared["cycle"] = shared
    assert retained_bytes(shared, shared) == retained_bytes(shared)


def test_lru_admission_respects_session_and_global_limits() -> None:
    budget = ReplayCacheBudget(100, 80)
    assert budget.update("a", 40) == []
    assert budget.update("b", 40) == []
    budget.touch("a")
    assert budget.update("c", 40) == ["b"]
    assert budget.total_bytes == 80
    assert budget.update("a", 81) == ["a"]
    assert budget.total_bytes == 40
    budget.discard("c")
    assert budget.total_bytes == 0


def test_terminal_frame_is_not_evidence_of_persistence() -> None:
    registry = SessionStreamRegistry(replay_cache_bytes=0, session_replay_cache_bytes=0)
    registry.record("a", "session.event.text_delta", {"task_id": "t", "text": "x" * 5000})
    registry.record("a", "session.event.done", {"task_id": "t"})
    # Taking the terminal handoff still happens BEFORE the storage commit.
    registry.take_terminal_activity_snapshot("a", "t", turn_id="t")
    assert registry.replay("a", 0).replay_complete
    assert registry.replay_cache_usage()["protected_bytes"] > 5000
    registry.mark_terminal_persisted("a", "t", reconstructible=True)
    result = registry.replay("a", 0)
    assert not result.replay_complete
    assert result.gap_reason == "buffer_window_missed"
    assert registry.current_seq("a") == 2
    assert registry.replay("a", 2).replay_complete


def test_live_turn_and_manual_compaction_remain_protected() -> None:
    registry = SessionStreamRegistry(replay_cache_bytes=0, session_replay_cache_bytes=0)
    registry.record("a", "session.event.text_delta", {"task_id": "t", "text": "active"})
    registry.mark_terminal_persisted("a", "previous", reconstructible=True)
    assert registry.live_snapshot("a").events
    assert registry.replay("a", 0).replay_complete
    registry.record("a", "session.event.compaction", {
        "source": "manual", "compaction_id": "c", "status": "started", "epoch": 1,
    })
    registry.record("a", "session.event.done", {"task_id": "t"})
    registry.mark_terminal_persisted("a", "t", reconstructible=True)
    assert registry.replay("a", 0).replay_complete
    registry.record("a", "session.event.compaction", {
        "source": "manual", "compaction_id": "c", "status": "completed", "epoch": 1,
    })
    assert registry.replay("a", 0).gap_reason == "buffer_window_missed"


def test_eviction_before_reset_cannot_claim_a_complete_replay() -> None:
    registry = SessionStreamRegistry(replay_cache_bytes=0, session_replay_cache_bytes=0)
    finish(registry, "a", "t")
    floor = registry.current_seq("a")
    registry.record("a", "session.event.answer_generation_reset", {
        "task_id": "next", "new_generation_epoch": 2,
    })
    registry.record("a", "session.event.text_delta", {
        "task_id": "next", "generation_epoch": 2, "text": "next answer",
    })
    assert registry.replay("a", 0).gap_reason == "buffer_window_missed"
    assert registry.replay("a", floor).replay_complete
    assert registry.current_seq("a") == floor + 2


def test_new_task_cannot_make_an_unpersisted_predecessor_reclaimable() -> None:
    registry = SessionStreamRegistry(replay_cache_bytes=0, session_replay_cache_bytes=0)
    registry.record("a", "session.event.text_delta", {"task_id": "old", "text": "old"})
    registry.record("a", "session.event.done", {"task_id": "old"})
    finish(registry, "a", "new")
    assert registry.replay("a", 0).replay_complete
    registry.mark_terminal_persisted("a", "old", reconstructible=True)
    assert registry.replay("a", 0).gap_reason == "buffer_window_missed"


def test_committed_event_needs_durable_recovery_proof_before_cache_admission() -> None:
    registry = SessionStreamRegistry(replay_cache_bytes=0, session_replay_cache_bytes=0)
    generation = registry.stream_generation
    registry.record("a", "session.event.text_delta", {"task_id": "t", "text": "x"})
    registry.record("a", TURN_COMMITTED_EVENT, {"task_id": "t"})
    assert registry.replay("a", 0).replay_complete
    registry.mark_terminal_persisted("a", "t")
    assert registry.replay("a", 0).replay_complete
    registry.mark_terminal_persisted("a", "t", reconstructible=True)
    assert registry.replay("a", 0).gap_reason == "buffer_window_missed"
    assert registry.stream_generation == generation
    registry.evict("a")
    assert registry.current_seq("a") == 0
    assert registry.replay_cache_usage()["reclaimable_bytes"] == 0


def test_many_durable_sessions_stay_within_byte_budget() -> None:
    registry = SessionStreamRegistry(replay_cache_bytes=16_384, session_replay_cache_bytes=8192)
    for index in range(500):
        finish(registry, str(index), f"task-{index}", "x" * 2048)
        assert registry.replay_cache_usage()["reclaimable_bytes"] <= 16_384
    assert registry.replay_cache_usage()["evictions"] > 0
    assert registry.replay("0", 0).gap_reason == "buffer_window_missed"
    assert registry.replay("499", 0).replay_complete


@pytest.mark.parametrize("proof", ["missing", "incomplete", "wrong-task", "complete"])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_terminal_storage_commit_requires_complete_recovery_evidence(
    monkeypatch: pytest.MonkeyPatch, proof: str, write_fails: bool,
) -> None:
    from opensquilla.gateway import session_streams
    from opensquilla.gateway.task_runtime import TaskRuntime

    registry = SessionStreamRegistry(replay_cache_bytes=0, session_replay_cache_bytes=0)
    monkeypatch.setattr(session_streams, "_session_streams", registry)
    registry.record("a", "session.event.text_delta", {"task_id": "t", "text": "durable text"})
    registry.record("a", "session.event.done", {"task_id": "t"})
    snapshot = registry.take_terminal_activity_snapshot("a", "t", turn_id="t", terminal_at=1)
    assert snapshot is not None and snapshot["complete"] is True
    if proof == "missing":
        snapshot = None
    elif proof == "incomplete":
        snapshot = {**snapshot, "complete": False}
    elif proof == "wrong-task":
        snapshot = {**snapshot, "task_id": "another-task"}

    class Storage:
        async def settle_agent_task(self, *_args: Any, **_kwargs: Any) -> None:
            if write_fails:
                raise OSError("synthetic write failure")

    runtime = TaskRuntime(storage=Storage(), turn_handler=AsyncMock())
    update = {"status": "succeeded", "finished_at": 1, "details": {"activity_snapshot": snapshot}}
    if write_fails:
        with pytest.raises(OSError, match="synthetic write failure"):
            await runtime._persist_terminal_update("t", "a", update)
    else:
        await runtime._persist_terminal_update("t", "a", update)
    # Even the public committed event cannot substitute for storage evidence.
    registry.record("a", TURN_COMMITTED_EVENT, {"task_id": "t"})
    assert registry.replay("a", 0).replay_complete == (write_fails or proof != "complete")
