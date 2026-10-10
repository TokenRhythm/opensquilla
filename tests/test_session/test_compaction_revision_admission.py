"""Consumer admission, finite draft replanning and exact source preservation."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import ChatConfig, Message
from opensquilla.session.compaction import CompactionRequest, compact_context
from opensquilla.session.tokenizer import estimate_tokens
from tests.helpers.compaction import synthetic_compaction_config


def history():
    return [
        {
            "role": role,
            "content": f"Source position {index * 2 + offset}. "
            + "Earlier discussion and completed work. " * 100,
        }
        for index in range(6)
        for offset, role in enumerate(("user", "assistant"))
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_proof", [True, False])
async def test_full_consumer_proof_replaces_local_capacity_gate(complete_proof):
    entries = history()
    summary = "Earlier decisions remain applicable. " * 300
    cfg = synthetic_compaction_config(summary=summary, protect_semantic_tail=False)
    consumer = OpenAIProvider(api_key="synthetic", model="synthetic-model")
    proofs = []

    def admission(checkpoint, kept):
        proof = consumer.project_final_request(
            [Message(role="user", content=checkpoint)],
            None,
            ChatConfig(provider_context_window_tokens=100_000, max_tokens=128),
        )
        proofs.append(proof)
        assert kept == []
        return proof

    result = await compact_context(
        CompactionRequest(
            session_id="complete-proof",
            entries=entries,
            config=cfg,
            context_window_tokens=500,
            context_window_chars=2000,
            forced_prefix_cut=len(entries),
            consumer_admission=admission if complete_proof else None,
        )
    )
    if complete_proof:
        assert proofs and all(proof.fits for proof in proofs)
        assert result.summary == summary.strip()
        assert result.tokens_after > 1024 and result.tokens_after > 500
        assert result.quality_report["fits_context_window"] is False
        assert result.quality_report["passes_structural_gate"] is True
        assert result.quality_report["capacity_verdict_source"] == "consumer_request"
        assert len(cfg.llm_plan.primary.provider.calls) == 1
    else:
        assert not result.summary
        assert result.skip_reason == "summary_does_not_fit"
        assert result.kept_entries == entries and result.removed_count == 0


@pytest.mark.asyncio
async def test_revision_with_more_characters_and_fewer_tokens_reaches_consumer(monkeypatch):
    entries = history()
    initial = "复杂约束必须继续保留。" * 100
    revised = ("Prior work and constraints remain applicable. " * 50).strip()
    assert len(revised) > len(initial)
    assert estimate_tokens(revised) < estimate_tokens(initial)
    summarize = AsyncMock(side_effect=[initial, revised])
    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_provider", summarize)
    cfg = synthetic_compaction_config(protect_semantic_tail=False)
    consumer = OpenAIProvider(api_key="synthetic", model="synthetic-model")
    proofs = []

    def admission(checkpoint, kept):
        assert not kept
        proof = consumer.project_final_request(
            [Message(role="user", content=checkpoint)],
            None,
            ChatConfig(provider_context_window_tokens=1500, max_tokens=128),
        )
        proofs.append(proof)
        return proof

    result = await compact_context(
        CompactionRequest(
            session_id="token-smaller-revision",
            entries=entries,
            config=cfg,
            context_window_tokens=1500,
            forced_prefix_cut=len(entries),
            consumer_admission=admission,
        )
    )
    assert summarize.await_count == 2
    assert len(proofs) == 2 and not proofs[0].fits and proofs[1].fits
    assert result.summary == revised.strip() and result.removed_count == len(entries)
    assert result.quality_report["passes_structural_gate"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("forced", [False, True])
async def test_capacity_replan_absorbs_only_new_complete_rounds(monkeypatch, forced):
    entries = history()
    frozen = deepcopy(entries)
    # Deliberately retain too much history on the first pass. Subsequent
    # admission models a wire envelope with room for four raw messages.
    monkeypatch.setattr("opensquilla.session.compaction._find_turn_boundary_cut", lambda *a, **k: 2)
    cfg = synthetic_compaction_config(
        summary="Earlier work is complete.",
        protected_recent_messages=4,
    )
    seen = []

    def admission(checkpoint, kept):
        seen.append(len(kept))
        return len(kept) <= 4

    result = await compact_context(
        CompactionRequest(
            session_id="replan",
            entries=entries,
            config=cfg,
            context_window_tokens=2000,
            forced_prefix_cut=2 if forced else None,
            consumer_admission=admission,
        )
    )
    assert entries == frozen
    if forced:
        assert not result.summary and result.kept_entries == entries
        assert set(seen) == {10}
    else:
        assert result.summary and result.removed_count == 8
        assert result.kept_entries == entries[8:]
        assert result.quality_report["replan_count"] > 0
        assert seen[0] == 10 and seen[-1] == 4
        calls = cfg.llm_plan.primary.provider.calls
        # Every removed source row is sent exactly once; later calls also
        # contain the prior complete checkpoint, not the old raw source.
        raw = [
            message.content
            for messages, _, _ in calls
            for message in messages
            if any(message.content == entry["content"] for entry in entries)
        ]
        assert raw == [entry["content"] for entry in entries[:8]]
        source_calls = [
            messages
            for messages, _, _ in calls
            if any(message.content in raw for message in messages)
        ]
        assert len(source_calls) > 1
        assert all(
            any("Earlier work is complete." in str(message.content) for message in messages)
            for messages in source_calls[1:]
        )


@pytest.mark.asyncio
async def test_indivisible_oversized_round_skips_useless_checkpoint_shortening():
    # One API round larger than the summarize deployment's whole input window.
    # No checkpoint length can admit it, so the operation must not spend a
    # model call shortening the rolling checkpoint before giving up — this
    # failure replays identically on every later turn of the same session.
    entries = [
        {"role": "user", "content": "Early question one. " + "ordinary detail " * 40},
        {"role": "assistant", "content": "Early answer one. " + "ordinary detail " * 40},
        {"role": "user", "content": "Early question two. " + "ordinary detail " * 40},
        {"role": "assistant", "content": "Early answer two. " + "ordinary detail " * 40},
        {"role": "user", "content": "Giant automation transcript. " + "payload token " * 60000},
        {"role": "assistant", "content": "Giant automation reply."},
        {"role": "user", "content": "Current question."},
        {"role": "assistant", "content": "Current answer."},
    ]
    cfg = synthetic_compaction_config(
        summary="Earlier rounds are complete.",
        protected_recent_messages=2,
        protect_semantic_tail=False,
    )

    result = await compact_context(
        CompactionRequest(
            session_id="indivisible-oversized-round",
            entries=entries,
            config=cfg,
            context_window_tokens=500,
        )
    )

    assert result.skip_reason == "summary_failed"
    assert result.failure_kind == "indivisible_round_exceeds_input"
    assert not result.summary and result.removed_count == 0
    assert result.kept_entries == entries
    # Exactly one summarize call for the fitting prefix; the checkpoint
    # shortening call that cannot change the outcome is not issued.
    assert len(cfg.llm_plan.primary.provider.calls) == 1


@pytest.mark.asyncio
async def test_replan_exhaustion_leaves_original_history_and_checkpoint(monkeypatch):
    entries = history()
    monkeypatch.setattr("opensquilla.session.compaction._find_turn_boundary_cut", lambda *a, **k: 2)
    cfg = synthetic_compaction_config(summary="Prior work completed.", protected_recent_messages=4)
    result = await compact_context(
        CompactionRequest(
            session_id="reject",
            entries=entries,
            config=cfg,
            context_window_tokens=2000,
            previous_summary="Previous checkpoint.",
            consumer_admission=lambda *_: False,
        )
    )
    assert not result.summary and result.removed_count == 0
    assert result.kept_entries == entries
    assert result.skip_reason == "consumer_admission_failed"
    assert len(cfg.llm_plan.primary.provider.calls) <= 8


@pytest.mark.asyncio
async def test_capacity_proof_does_not_waive_actual_shrink_requirement():
    entries = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    cfg = synthetic_compaction_config(
        summary="Much longer than the source. " * 100, protect_semantic_tail=False
    )
    result = await compact_context(
        CompactionRequest(
            session_id="no-benefit",
            entries=entries,
            config=cfg,
            context_window_tokens=100,
            forced_prefix_cut=2,
            consumer_admission=lambda *_: True,
        )
    )
    assert result.skip_reason == "no_compression_benefit"
    assert not result.summary and result.kept_entries == entries


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", [False, True])
@pytest.mark.parametrize("previous", [None, "Prior short checkpoint."])
async def test_cached_usage_cannot_authorize_growing_replay(tail, previous):
    entries = [
        {"role": "user", "content": "Earlier short question.", "token_count": 20000},
        {"role": "assistant", "content": "Short reply.", "token_count": 20000},
    ]
    if tail:
        entries += [
            {"role": "user", "content": "Current task.", "token_count": 40000},
            {"role": "assistant", "content": "Current reply.", "token_count": 40000},
        ]
    cfg = synthetic_compaction_config(
        summary="An expanded explanation of the earlier work. " * 100,
        protect_semantic_tail=False,
    )
    result = await compact_context(
        CompactionRequest(
            session_id="cached-usage",
            entries=entries,
            config=cfg,
            context_window_tokens=200000,
            previous_summary=previous,
            forced_prefix_cut=2,
            consumer_admission=lambda *_: True,
        )
    )
    assert result.skip_reason == "no_compression_benefit"
    assert result.kept_entries == entries and not result.summary
    assert (
        result.quality_report["replay_tokens_after"] > result.quality_report["replay_tokens_before"]
    )
    assert result.quality_report["replay_tokens_before"] < 100
