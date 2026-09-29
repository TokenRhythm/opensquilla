"""Expanded compaction drafts retain live rounds and the original call budget."""

from copy import deepcopy
from dataclasses import replace

import pytest

from opensquilla.session.compaction import CompactionRequest, compact_context
from tests.helpers.compaction import synthetic_compaction_config


def _old_rows():
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"Unique row {index}: " + "Earlier completed discussion. " * 100,
        }
        for index in range(12)
    ]


@pytest.mark.parametrize("status", ["pending", "running"])
async def test_replan_keeps_unfinished_tool_round_raw(monkeypatch, status):
    entries = _old_rows()[:8] + [
        {"role": "user", "content": "Current exact task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "running-tool", "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        },
        {
            "role": "tool", "tool_call_id": "running-tool",
            "content": '{"status":"' + status + '"}',
        },
    ]
    before = deepcopy(entries)
    monkeypatch.setattr(
        "opensquilla.session.compaction._find_turn_boundary_cut", lambda *a, **k: 2,
    )
    cfg = synthetic_compaction_config(summary="A complete checkpoint.")
    result = await compact_context(CompactionRequest(
        session_id="replan-live", entries=entries, config=cfg,
        context_window_tokens=2000, consumer_admission=lambda _, kept: len(kept) <= 3,
    ))
    assert result.quality_report["replan_count"] > 0
    assert result.removed_count == 8 and result.kept_entries == entries[8:]
    assert entries == before
    source_contents = {entry["content"] for entry in entries[:8]}
    observed = [
        message.content
        for messages, _, _ in cfg.llm_plan.primary.provider.calls
        for message in messages
        if message.content in source_contents
    ]
    assert observed == [entry["content"] for entry in entries[:8]]


async def test_replan_keeps_original_call_limit_and_deadline(monkeypatch):
    entries = _old_rows()
    before = deepcopy(entries)
    monkeypatch.setattr(
        "opensquilla.session.compaction._find_turn_boundary_cut", lambda *a, **k: 2,
    )
    cfg = synthetic_compaction_config(
        summary="A complete checkpoint.", protected_recent_messages=4,
    )
    cfg.llm_plan = replace(cfg.llm_plan, max_calls=2)
    result = await compact_context(CompactionRequest(
        session_id="replan-budget", entries=entries, config=cfg,
        context_window_tokens=2000, previous_summary="Prior checkpoint.",
        consumer_admission=lambda *_: False,
    ))
    calls = cfg.llm_plan.primary.provider.calls
    assert len(calls) == cfg.llm_calls_started == 2
    assert all(config.turn_deadline_at_monotonic == cfg.deadline_at_monotonic
               for _, _, config in calls)
    assert result.removed_count == 0 and not result.summary
    assert result.kept_entries == entries == before
