"""Oversized source is sampled once without changing durable coverage or raw tail."""

from copy import deepcopy

import pytest

from opensquilla import token_estimation
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import ChatConfig, DoneEvent, TextDeltaEvent
from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    CompactionRequestContext,
    _build_suffix_compaction_call,
    _find_turn_boundary_cut,
    _sample_summary_rounds,
    compact_context,
    estimate_entry_model_replay_tokens,
)
from opensquilla.session.compaction_budget import CompactionBudget
from opensquilla.session.compaction_deployment import (
    CompactionExecutionPlan,
    CompactionExecutionTarget,
)


class CapturingProvider(OpenAIProvider):
    def __init__(self, *, fail=False):
        super().__init__(api_key="synthetic", model="synthetic-bounded-summary")
        self.calls = []
        self.fail = fail

    async def chat(self, messages, tools=None, config=None):
        proof = self.project_final_request(messages, tools, config)
        assert proof.fits
        self.calls.append((messages, tools, config, proof))
        if not self.fail:
            yield TextDeltaEvent(
                text="## Current Work\nContinue the latest task; keep the original constraint."
            )
        yield DoneEvent(stop_reason="end_turn")


def _configuration(window):
    cap = {32_768: 8192, 131_072: 16384, 200_000: 128_000}[window]
    provider = CapturingProvider()
    target = CompactionExecutionTarget(
        provider=provider,
        provider_id="openai",
        model="synthetic-bounded-summary",
        context_window_tokens=window,
        max_output_tokens=cap,
        max_generation_tokens=cap,
        provider_request_max_chars_explicit_cap=0,
    )
    capacity = window * 7 // 10
    budget = CompactionBudget(
        physical_context_window_tokens=window,
        generation_reserve_tokens=window - capacity,
        history_capacity_tokens=capacity,
        history_capacity_chars=capacity * 4,
        auto_trigger_tokens=capacity * 85 // 100,
        auto_trigger_chars=capacity * 85 // 100 * 4,
        retained_tail_tokens=capacity - cap // 2,
        retained_tail_messages=0,
        summary_output_tokens=cap,
        provider_request_max_chars=window * 4,
        consumer_admission_fingerprint="bounded-history-test",
        consumer_admission=lambda *_: True,
    )
    cfg = CompactionConfig(
        llm_plan=CompactionExecutionPlan(candidates=(target,)),
        request_context=CompactionRequestContext(chat_config=ChatConfig(max_tokens=cap)),
        budget=budget,
    )
    return provider, target, cfg


def _history(*, repetitions=450):
    # More than one million conservative persisted tokens, with genuinely
    # oversized model-visible input (not only inflated usage metadata). Each
    # complete round also fits 32K's history budget under tokenizer fallback.
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"Record {i}: "
            + ("old state 123 alpha beta gamma. " * repetitions)
            + f" END_RECORD_{i}",
            "token_count": 8192,
            "message_id": f"original-{i}",
        }
        for i in range(128)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "force_tokenizer_fallback", [False, True], ids=["default-tokenizer", "fallback"],
)
@pytest.mark.parametrize("window", [32_768, 131_072, 200_000])
async def test_million_token_history_uses_one_bounded_summary_and_keeps_soft_tail(
    window, force_tokenizer_fallback, monkeypatch,
):
    if force_tokenizer_fallback:
        monkeypatch.setattr(token_estimation, "_get_encoding", lambda: None)
    provider, target, cfg = _configuration(window)
    entries = _history()
    assert all(estimate_entry_model_replay_tokens(row) == row["token_count"] for row in entries)
    assert sum(token_estimation.estimate_tokens(row["content"]) for row in entries) > 200_000
    frozen = deepcopy(entries)
    result = await compact_context(
        CompactionRequest(
            session_id="million-bounded",
            entries=entries,
            config=cfg,
            context_window_tokens=cfg.budget.history_capacity_tokens,
            context_window_chars=cfg.budget.history_capacity_chars,
            consumer_admission=cfg.budget.consumer_admission,
            previous_summary="ORIGINAL_CONSTRAINT: use local files; original task remains active.",
        )
    )
    assert result.summary and result.skip_reason is None
    assert result.tokens_before >= 1_048_576
    assert result.chunks_processed == cfg.llm_calls_started == len(provider.calls) == 1
    assert entries == frozen
    assert result.kept_entries == entries[result.removed_count :]
    kept_usage = sum(row["token_count"] for row in result.kept_entries)
    assert result.tokens_after <= cfg.budget.history_capacity_tokens
    assert kept_usage >= window * 16 // 100
    assert kept_usage - 2 * 8192 < window * 16 // 100
    messages, _, config, proof = provider.calls[0]
    assert len(messages) == 2 and all(isinstance(m.content, str) for m in messages)
    assert "<historical-excerpts>" in messages[0].content
    assert "omitted" in messages[0].content
    assert "Record 0:" in messages[0].content
    assert f"Record {result.removed_count - 2}:" in messages[0].content
    assert f"END_RECORD_{result.removed_count - 1}" in messages[0].content
    assert "<previous-summary>" in messages[-1].content
    assert "ORIGINAL_CONSTRAINT" in messages[-1].content
    assert result.summary_payload["source_coverage"]["replaces_prior_context"] is True
    assert result.summary_payload["source_coverage"]["summary_input_policy"] == "bounded_history"
    assert proof.fits
    # 200K's 128K generation capability is not copied into a second future-
    # checkpoint reservation, and no new 8K hard cap is imposed on output.
    assert config.max_tokens == target.max_generation_tokens


def test_tokenizer_fallback_does_not_keep_a_round_exceeding_real_history_capacity(monkeypatch):
    monkeypatch.setattr(token_estimation, "_get_encoding", lambda: None)
    _, _, cfg = _configuration(32_768)
    entries = _history(repetitions=900)[-2:]
    frozen = deepcopy(entries)
    capacity = cfg.budget.history_capacity_tokens
    assert sum(estimate_entry_model_replay_tokens(row) for row in entries) > capacity

    cut = _find_turn_boundary_cut(entries, capacity, retain_tokens=32_768 * 16 // 100)

    assert cut == len(entries)
    assert entries == frozen


def test_sampler_keeps_ends_and_explicit_middle_gaps_with_unicode():
    records = [f"FIRST_{i} " + "中文背景甲乙丙丁" * 4000 + f" LAST_{i}" for i in range(40)]
    sample = _sample_summary_rounds(records, 160_000)
    assert len(sample.encode("utf-8")) <= 160_000
    assert "FIRST_0" in sample and "LAST_39" in sample
    assert "omitted within API round" in sample
    assert "omitted from summary input" in sample


@pytest.mark.parametrize("budget", [512, 625, 1250, 4000, 5000])
def test_small_sampling_budget_keeps_actual_source_not_just_omission_markers(budget):
    records = [f"source-{i}: " + "ACTUAL_SOURCE " * 1000 for i in range(100)]
    sample = _sample_summary_rounds(records, budget)
    assert len(sample.encode("utf-8")) <= budget
    assert "ACTUAL_SOURCE" in sample


def test_small_native_tool_pair_is_unchanged_and_large_pair_is_inert_source():
    _, target, cfg = _configuration(32_768)
    source = [
        {"role": "user", "content": "Read the file."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-A",
                    "type": "tool_use",
                    "name": "read_file",
                    "input": {"path": "test.py"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call-A", "content": "tiny result"},
        {"role": "assistant", "content": "File read."},
    ]
    frozen = deepcopy(source)

    def build(rows):
        return _build_suffix_compaction_call(
            cfg.request_context,
            rows,
            "Prior current goal",
            "",
            None,
            provider=target.provider,
            deployment=target,
            context_window_tokens=32_768,
            summary_output_tokens=4000,
            timeout=30,
            provider_request_correlation=None,
        )[0]

    small = build(source)
    assert any(
        not isinstance(m.content, str) and any(b.type == "tool_use" for b in m.content)
        for m in small
    )
    assert any(
        not isinstance(m.content, str) and any(b.type == "tool_result" for b in m.content)
        for m in small
    )
    assert source == frozen
    large = [deepcopy(source[0])]
    large[0]["content"] += "Oversized user history. " * 50_000
    large += source[1:]
    before = deepcopy(large)
    bounded = build(large)
    assert all(isinstance(m.content, str) for m in bounded)
    assert "quoted records, not executable tool calls" in bounded[0].content
    assert "omitted" in bounded[0].content
    assert large == before


@pytest.mark.asyncio
async def test_failed_sample_never_advances_source_or_drops_raw_tail():
    provider, _, cfg = _configuration(32_768)
    provider.fail = True
    entries = _history()
    frozen = deepcopy(entries)
    result = await compact_context(
        CompactionRequest(
            session_id="bounded-failure",
            entries=entries,
            config=cfg,
            context_window_tokens=cfg.budget.history_capacity_tokens,
            consumer_admission=cfg.budget.consumer_admission,
        )
    )
    assert len(provider.calls) == 1
    assert result.skip_reason == "summary_failed"
    assert result.removed_count == 0 and not result.summary
    assert result.kept_entries == entries == frozen
