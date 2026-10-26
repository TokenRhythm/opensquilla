"""Skip only checkpoint revisions disproved by an exact consumer tail proof."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import ChatConfig, Message
from opensquilla.session.compaction import CompactionRequest, compact_context
from opensquilla.session.compaction_budget import resolve_compaction_budget
from opensquilla.session.compaction_lifecycle import ConsumerAdmissionStaleError
from tests.helpers.compaction import synthetic_compaction_config


def envelope(*, cap=4_000, project_tail=lambda entry: entry["content"]):
    provider = OpenAIProvider(api_key="synthetic", model="synthetic-model")
    config = ChatConfig(
        max_tokens=512, provider_context_window_tokens=100_000,
        provider_request_max_chars=cap, provider_request_max_chars_explicit_cap=cap,
        system="Continue the current task.",
    )

    def project(summary, kept):
        return provider.project_final_request(
            [Message(role="user", content=summary)]
            + [Message(role="user", content=project_tail(entry)) for entry in kept],
            None, config,
        )

    return config, project, resolve_compaction_budget(
        project=project, physical_context_window_tokens=100_000,
        generation_reserve_tokens=512, request_character_limit=cap,
    )


def rows(count=4, *, chars=2_000):
    return [
        {"role": "user" if index % 2 == 0 else "assistant",
         "content": f"Completed source {index}: " + "a" * chars}
        for index in range(count)
    ]


def test_tail_proof_is_exact_hard_capacity_not_soft_history_watermark():
    _, project, budget = envelope(cap=10_000)
    kept = rows(2, chars=4_000)
    # Soft scheduling can be arbitrarily lower without changing actual fit.
    budget = replace(budget, history_capacity_chars=20, history_capacity_tokens=5)
    assert project("", kept).fits
    assert not budget.consumer_tail_over_capacity(kept)
    assert budget.consumer_tail_over_capacity(rows(2, chars=6_000))


@pytest.mark.parametrize("case", ["missing", "unknown", "message_count", "stale"])
def test_rejected_or_unavailable_tail_proof_is_not_capacity_evidence(case):
    config, project, _ = envelope()

    def faulty_project(summary, kept):
        candidate = project(summary, kept)
        if not kept:
            return candidate
        if case == "missing":
            return None
        if case == "unknown":
            return SimpleNamespace(payload={}, proof={}, fits=False)
        return replace(candidate, fits=False, fits_message_count=False)

    budget = resolve_compaction_budget(
        project=faulty_project, physical_context_window_tokens=100_000,
        generation_reserve_tokens=512,
    )
    if case == "stale":
        config.system = "The actual request envelope has changed."
        with pytest.raises(ConsumerAdmissionStaleError):
            budget.consumer_tail_over_capacity(rows(2, chars=100))
    else:
        assert not budget.consumer_tail_over_capacity(rows(2, chars=100))


@pytest.mark.asyncio
async def test_huge_source_tail_with_small_consumer_projection_still_shortens(monkeypatch):
    entries = rows(4, chars=8_000)
    original = deepcopy(entries)
    # Models a verified tool-result projection: the consumer actually receives
    # a short reference even though the authoritative source remains large.
    _, project, budget = envelope(project_tail=lambda entry: entry["content"][:80])
    assert sum(len(entry["content"]) for entry in entries[2:]) > 4_000
    assert project("", entries[2:]).fits
    assert not budget.consumer_tail_over_capacity(entries[2:])
    summarize = AsyncMock(side_effect=["Large draft. " * 800, "Completed earlier work."])
    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_provider", summarize)
    cfg = synthetic_compaction_config(budget=budget)
    result = await compact_context(CompactionRequest(
        session_id="projected-tail", entries=entries, config=cfg,
        context_window_tokens=50_000, context_window_chars=100,
        forced_prefix_cut=2, consumer_admission=budget.consumer_admission,
    ))
    assert summarize.await_count == 2
    assert result.summary and result.removed_count == 2
    assert result.kept_entries == entries[2:] and entries == original


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_empty_summary_rejection_does_not_cancel_effective_revision(monkeypatch, bound):
    entries = rows()
    summarize = AsyncMock(side_effect=["Large draft. " * 800, "Completed earlier work."])
    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_provider", summarize)

    def admission(summary, kept):
        return bool(summary) and len(summary) < 200

    _, project, budget = envelope(project_tail=lambda entry: entry["content"][:80])
    if bound:
        def reject_empty(summary, kept):
            if not summary and kept:
                return None
            return project(summary, kept)

        budget = resolve_compaction_budget(
            project=reject_empty, physical_context_window_tokens=100_000,
            generation_reserve_tokens=512,
        )
        admission = budget.consumer_admission
    else:
        # A different callback cannot inherit the original consumer's proof.
        budget = replace(budget, consumer_tail_over_capacity=lambda kept: True)
    cfg = synthetic_compaction_config(budget=budget)
    result = await compact_context(CompactionRequest(
        session_id="custom-admission", entries=entries, config=cfg,
        context_window_tokens=50_000, forced_prefix_cut=2, consumer_admission=admission,
    ))
    assert not admission("", entries[2:])
    assert summarize.await_count == 2
    assert result.summary and result.removed_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("forced", [False, True])
async def test_exact_tail_overflow_skips_revision_but_preserves_cut_advance(monkeypatch, forced):
    entries = rows(10, chars=1_000)
    original = deepcopy(entries)
    _, _, budget = envelope(cap=3_000)
    budget = replace(budget, auto_trigger_tokens=1, auto_trigger_chars=1)
    monkeypatch.setattr("opensquilla.session.compaction._find_turn_boundary_cut", lambda *a, **k: 2)
    summarize = AsyncMock(return_value="Completed earlier work.")
    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_provider", summarize)
    cfg = synthetic_compaction_config(budget=budget, protected_recent_messages=2)
    result = await compact_context(CompactionRequest(
        session_id="tail-replan", entries=entries, config=cfg,
        context_window_tokens=50_000, consumer_admission=budget.consumer_admission,
        forced_prefix_cut=2 if forced else None,
    ))
    source_counts = [
        len(call.kwargs.get("source_entries") or []) for call in summarize.await_args_list
    ]
    assert source_counts == ([2] if forced else [2, 2, 2, 2])
    assert entries == original
    if forced:
        assert not result.summary and result.removed_count == 0
        assert result.kept_entries == original
    else:
        assert result.summary and result.removed_count == 8
        assert result.kept_entries == original[8:]
        assert result.quality_report["replan_count"] == 3


@pytest.mark.asyncio
async def test_stale_tail_probe_stops_without_extra_summary_or_false_commit(monkeypatch):
    entries = rows()
    config, _, budget = envelope()
    calls = 0

    def admission(summary, kept):
        nonlocal calls
        calls += 1
        config.system = "Changed after the candidate proof."
        return False

    budget = replace(budget, consumer_admission=admission)
    summarize = AsyncMock(return_value="Complete portable checkpoint.")
    monkeypatch.setattr("opensquilla.session.compaction.call_compaction_provider", summarize)
    cfg = synthetic_compaction_config(budget=budget)
    result = await compact_context(CompactionRequest(
        session_id="stale-tail", entries=entries, config=cfg, context_window_tokens=50_000,
        forced_prefix_cut=2, consumer_admission=budget.consumer_admission,
    ))
    assert calls == summarize.await_count == 1
    assert result.skip_reason == "consumer_admission_stale"
    assert not result.summary and result.removed_count == 0 and result.kept_entries == entries
