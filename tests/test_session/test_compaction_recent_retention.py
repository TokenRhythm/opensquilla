"""Automatic retention is a consumer-window target, not a new safety gate."""

from copy import deepcopy

import pytest

from opensquilla.session.compaction import (
    CompactionRequest,
    _find_turn_boundary_cut,
    compact_context,
)
from opensquilla.session.compaction_budget import CompactionBudget
from tests.helpers.compaction import synthetic_compaction_config


def _budget(window=32_768, *, capacity=None, trigger=None):
    capacity = capacity if capacity is not None else window * 60 // 100
    trigger = trigger if trigger is not None else window // 2
    return CompactionBudget(
        physical_context_window_tokens=window,
        generation_reserve_tokens=4096,
        history_capacity_tokens=capacity,
        history_capacity_chars=capacity * 4,
        auto_trigger_tokens=trigger,
        auto_trigger_chars=trigger * 4,
        retained_tail_tokens=capacity - 4096,
        retained_tail_messages=0,
        summary_output_tokens=4096,
        provider_request_max_chars=window * 4,
        consumer_admission_fingerprint="retention-fixture",
        consumer_admission=lambda *_: True,
    )


def _rounds(window=32_768, count=6):
    # Persisted usage is intentionally conservative. The existing replay
    # estimator honors this surplus for planning; actual source prose is kept
    # small so the 1M planning case needs no huge input or external API.
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"Completed discussion {index}. " + "Earlier details. " * 180,
            "token_count": window * 6 // 100,
        }
        for index in range(count * 2)
    ]


async def _compact(entries, budget, **kwargs):
    config = kwargs.pop("config", synthetic_compaction_config(budget=budget))
    return await compact_context(CompactionRequest(
        session_id="recent-retention",
        entries=entries,
        config=config,
        context_window_tokens=budget.history_capacity_tokens,
        context_window_chars=budget.history_capacity_chars,
        consumer_admission=budget.consumer_admission,
        **kwargs,
    ))


@pytest.mark.parametrize("window", [32_768, 131_072, 200_000, 1_000_000])
async def test_automatic_keeps_recent_complete_round_from_consumer_window(window):
    entries = _rounds(window)
    before = deepcopy(entries)
    budget = _budget(window)
    config = synthetic_compaction_config(budget=budget)
    # The summary model has its own 100K window, and the source history gets
    # only 60% of the consumer window. Neither is the retention denominator.
    assert config.llm_plan.primary.context_window_tokens == 100_000

    result = await _compact(entries, budget, config=config)

    # Each round is 12%: retain two complete rounds to reach the 16% target.
    assert result.summary and result.removed_count == 8
    assert result.kept_entries == entries[-4:]
    assert entries == before


async def test_sixteen_percent_retains_two_rounds_where_twenty_would_require_three():
    entries = _rounds()
    for entry in entries:
        # Each complete round is about 9% of the physical window. Two reach
        # the 16% target; reaching a 20% target would require three.
        entry["token_count"] = 32_768 * 45 // 1000

    result = await _compact(entries, _budget())

    assert result.summary and result.removed_count == 8
    assert result.kept_entries == entries[-4:]


async def test_previous_checkpoint_is_not_subtracted_twice_from_recent_share():
    entries = _rounds()
    previous = "Previously completed work. " * 600
    budget = _budget()

    result = await _compact(entries, budget, previous_summary=previous)

    # The old checkpoint fits beside the 16% raw tail within the history
    # allocation. Deducting it again from that tail would remove the last
    # complete round even though the full consumer request has ample room.
    assert result.summary and result.removed_count == 8
    assert result.kept_entries == entries[-4:]


async def test_retention_target_does_not_lower_the_pressure_trigger():
    entries = _rounds(count=2)
    budget = _budget()
    config = synthetic_compaction_config(budget=budget)

    result = await _compact(entries, budget, config=config)

    assert result.skip_reason == "within_compaction_budget"
    assert result.removed_count == 0 and result.kept_entries == entries
    assert not config.llm_plan.primary.provider.calls


async def test_character_heavy_history_has_no_synthetic_sixteen_percent_character_cap():
    entries = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": "x" * 5000}
        for index in range(16)
    ]
    budget = _budget()

    result = await _compact(entries, budget)

    # Repeated characters are cheap in tokens. Keep five complete rounds to
    # reach the token target, within the real history character allocation.
    assert result.summary and result.removed_count == 6
    assert result.kept_entries == entries[-10:]


@pytest.mark.parametrize("window,round_tokens", [(32_768, 8000), (131_072, 25_000)])
async def test_newest_completed_round_crosses_soft_target_without_being_dropped(
    window, round_tokens,
):
    entries = _rounds(window)
    for entry in entries:
        entry["token_count"] = round_tokens // 2
    entries.append({"role": "user", "content": "Continue the current task"})
    before = deepcopy(entries)

    result = await _compact(entries, _budget(window))

    assert result.summary and result.removed_count == 10
    assert result.kept_entries == entries[-3:]
    assert entries == before


@pytest.mark.parametrize("capacity,expected_cut", [(9000, 4), (7000, 6)])
def test_soft_target_never_overrides_the_existing_token_capacity(capacity, expected_cut):
    entries = _rounds(count=3)
    for entry in entries:
        entry["token_count"] = 4000

    assert _find_turn_boundary_cut(entries, capacity, retain_tokens=5242) == expected_cut


def test_soft_target_never_overrides_the_existing_character_capacity():
    entries = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": "x" * 5000}
        for index in range(16)
    ]

    cut = _find_turn_boundary_cut(entries, 20_000, 35_000, retain_tokens=5242)

    # Four rounds exceed the actual 35K character allocation. Keep only three
    # even though their tokens fall short of the target.
    assert cut == 10


async def test_completed_tool_pair_crossing_soft_target_is_kept_whole():
    entries = _rounds() + [
        {"role": "user", "content": "Look up the record", "token_count": 2000},
        {
            "role": "assistant", "content": "", "token_count": 3000,
            "tool_calls": [{"id": "finished-call", "type": "function", "function": {
                "name": "lookup", "arguments": "{}",
            }}],
        },
        {
            "role": "tool", "tool_call_id": "finished-call",
            "content": '{"status":"completed"}', "token_count": 3000,
        },
        {"role": "assistant", "content": "Lookup completed.", "token_count": 1000},
        {"role": "user", "content": "Continue the current task"},
    ]

    result = await _compact(entries, _budget())

    assert result.summary and result.removed_count == 12
    assert result.kept_entries == entries[-5:]


async def test_actual_admission_can_replan_below_the_soft_target():
    entries = _rounds()
    budget = _budget()
    admitted_tail_lengths = []

    def admission(_summary, kept):
        admitted_tail_lengths.append(len(kept))
        return len(kept) <= 2

    result = await compact_context(CompactionRequest(
        session_id="retention-replan",
        entries=entries,
        config=synthetic_compaction_config(budget=budget),
        context_window_tokens=budget.history_capacity_tokens,
        context_window_chars=budget.history_capacity_chars,
        consumer_admission=admission,
    ))

    assert admitted_tail_lengths[0] == 4
    assert result.summary and result.removed_count == 10
    assert result.kept_entries == entries[-2:]


async def test_tail_leaving_no_body_space_shrinks_before_spending_a_summary_call():
    entries = _rounds()
    for entry in entries:
        entry["token_count"] = 4000
    budget = _budget(200_000, capacity=8000, trigger=8000)
    config = synthetic_compaction_config(budget=budget)

    result = await _compact(entries, budget, config=config)

    # The initial 8K round fits the raw-tail guard but leaves no checkpoint
    # space. The existing pre-call shrink must advance beyond that round,
    # instead of trying to reach the 32K soft target again.
    assert result.summary and result.removed_count == len(entries)
    assert result.kept_entries == []
    assert len(config.llm_plan.primary.provider.calls) == 1


async def test_live_tool_round_remains_raw_even_above_recent_target():
    entries = _rounds() + [
        {"role": "user", "content": "Continue the current task", "token_count": 5000},
        {
            "role": "assistant", "content": "", "token_count": 5000,
            "tool_calls": [{"id": "live-call", "type": "function", "function": {
                "name": "lookup", "arguments": "{}",
            }}],
        },
        {
            "role": "tool", "tool_call_id": "live-call",
            "content": '{"status":"running"}', "token_count": 5000,
        },
    ]
    before = deepcopy(entries)

    result = await _compact(entries, _budget())

    assert result.summary and result.removed_count == 12
    assert result.kept_entries == entries[-3:]
    assert entries == before


@pytest.mark.parametrize("intent,removed", [({"force": True}, 12), ({"forced_prefix_cut": 2}, 2)])
async def test_explicit_selection_is_not_replaced_by_automatic_retention(intent, removed):
    entries = _rounds() + [{"role": "user", "content": "New current request"}]

    result = await _compact(entries, _budget(capacity=32_768), **intent)

    assert result.summary and result.removed_count == removed
    assert result.kept_entries == entries[removed:]


async def test_previous_checkpoint_keeps_existing_capacity_limit():
    entries = _rounds(20_000)
    previous = "Previously completed work. " * 1000
    budget = _budget(200_000, capacity=10_000, trigger=8000)

    result = await _compact(entries, budget, previous_summary=previous)

    # The physical 200K model does not authorize a 32K raw tail when the
    # actual history allocation, including the old checkpoint, is only 10K.
    assert result.summary and result.removed_count >= 10
    assert len(result.kept_entries) <= 2


async def test_failed_summary_preserves_full_source_and_previous_checkpoint():
    entries = _rounds()
    before = deepcopy(entries)
    budget = _budget()
    config = synthetic_compaction_config(summary="", budget=budget)

    result = await _compact(entries, budget, config=config, previous_summary="Prior checkpoint.")

    assert result.skip_reason == "summary_failed"
    assert result.removed_count == 0 and not result.summary
    assert not result.replaced_previous_summary
    assert result.kept_entries == entries == before


async def test_legacy_without_physical_consumer_budget_keeps_existing_policy():
    entries = _rounds()
    budget = _budget(capacity=20_000)
    config = synthetic_compaction_config()

    result = await _compact(entries, budget, config=config)

    # This legacy request only knows an effective history allocation. Treating
    # it as a physical model window would incorrectly discard extra history.
    assert result.summary and result.removed_count == 4
    assert result.kept_entries == entries[4:]
