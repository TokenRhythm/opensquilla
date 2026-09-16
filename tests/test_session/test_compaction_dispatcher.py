"""Tests for the compaction compaction public entry point.

: the dispatcher env-var, legacy implementation, and shadow comparator
have been deleted. ``compact_context`` delegates directly to
``compact_context_new``. These tests cover the public contract and the
turn-boundary cut behavior introduced in compaction.
"""

from __future__ import annotations

import pytest

from opensquilla.session.compaction import (
    CompactionRequest,
    CompactionResult,
    compact_context,
    compact_context_new,
)
from tests.helpers.compaction import synthetic_compaction_config


def _make_request(
    entries: list[dict] | None = None,
    window: int = 8192,
) -> CompactionRequest:
    if entries is None:
        entries = [
            {"role": "user", "content": "hello", "token_count": 5},
            {"role": "assistant", "content": "world", "token_count": 5},
        ]
    return CompactionRequest(
        session_id="dispatcher-test",
        entries=entries,
        context_window_tokens=window,
    )


# ---------------------------------------------------------------------------
# compact_context delegates to compact_context_new
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_context_returns_compaction_result():
    """compact_context returns a CompactionResult without raising."""
    result = await compact_context(_make_request())
    assert isinstance(result, CompactionResult)


@pytest.mark.asyncio
async def test_compact_context_delegates_to_new(monkeypatch):
    """compact_context calls compact_context_new (not any legacy path)."""
    import opensquilla.session.compaction as compaction_mod

    calls = []

    async def spy_new(request):
        calls.append(request)
        return CompactionResult(
            summary="spy",
            kept_entries=[],
            removed_count=0,
            chunks_processed=0,
            summary_source="skipped",
            tokens_before=0,
            tokens_after=0,
            remaining_budget_tokens=8192,
        )

    monkeypatch.setattr(compaction_mod, "compact_context_new", spy_new)
    result = await compact_context(_make_request())
    assert result.summary == "spy"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_compact_context_noop_when_within_budget():
    """When total tokens fit in the window, nothing is removed."""
    result = await compact_context(_make_request(window=8192))
    assert result.removed_count == 0
    assert result.summary_source == "skipped"


# ---------------------------------------------------------------------------
# compact_context_new: turn-boundary cut
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_avoids_mid_turn_cut():
    """Turn-boundary cut must not split an assistant tool_call from its result."""
    # The raw 250-token keep budget would retain the final answer, question,
    # and tool result but split it from its 200-token assistant call. The
    # completed tool round must instead move wholly into the summary.
    entries = [
        {"role": "user", "content": "Earlier request.", "token_count": 1_000},
        {"role": "assistant", "content": "Earlier answer.", "token_count": 1_000},
        {"role": "user", "content": "q1", "token_count": 200},
        {
            "role": "assistant",
            "content": '[tool_call:read_file({"path": "x"})]',
            "token_count": 200,
        },
        {"role": "tool", "content": "[tool_result:read_file] contents", "token_count": 100},
        {"role": "user", "content": "q2", "token_count": 5},
        {"role": "assistant", "content": "answer", "token_count": 5},
    ]
    request = CompactionRequest(
        session_id="boundary-test", entries=entries, context_window_tokens=500,
        config=synthetic_compaction_config(safety_margin=1.0),
    )
    result = await compact_context_new(request)

    assert result.removed_count == 5
    assert result.summary
    assert result.kept_entries == entries[5:]
    removed = entries[:result.removed_count]
    kept = result.kept_entries
    is_mid_turn = (
        removed[-1]["role"] == "assistant"
        and "[tool_call:" in removed[-1]["content"]
        and kept[0]["role"] == "tool"
    )
    assert not is_mid_turn


@pytest.mark.asyncio
async def test_new_avoids_mid_turn_cut_for_agent_flattened_tool_blocks():
    """Turn-boundary cut must match the Agent's flattened tool-use entries."""
    entries = [
        {"role": "user", "content": "old context", "token_count": 1_000},
        {"role": "user", "content": "q1", "token_count": 100},
        {"role": "assistant", "content": "[Used tool: read_file]", "token_count": 5},
        {
            "role": "user",
            "content": "[Tool result (toolu_1): file contents]",
            "token_count": 5,
        },
        {"role": "user", "content": "q2", "token_count": 5},
        {"role": "assistant", "content": "answer", "token_count": 5},
    ]
    request = CompactionRequest(
        session_id="agent-flattened-boundary-test",
        entries=entries,
        context_window_tokens=500,
        config=synthetic_compaction_config(safety_margin=1.0),
    )
    result = await compact_context_new(request)

    assert result.removed_count > 0
    removed = entries[: len(entries) - len(result.kept_entries)]
    kept = result.kept_entries
    assert removed[-1]["content"] != "[Used tool: read_file]"
    assert kept[0]["content"] == "q1"
    assert kept[1]["content"] == "[Used tool: read_file]"


@pytest.mark.asyncio
async def test_new_can_cut_after_completed_tool_round(monkeypatch):
    """A paired tool call/result is a safe boundary, not live protocol state.

    The branch under test sits in a two-token-wide window band: one token
    higher and the transcript is within budget, one lower and a cut is found
    and the quality gate rejects it. Pin token math so the band — and this test
    — stay deterministic and offline instead of loading an optional tokenizer.
    """
    monkeypatch.setattr(
        "opensquilla.session.compaction._estimate_tokens",
        lambda text: max(1, len(text) // 4),
    )
    entries = [
        {
            "role": "assistant",
            "content": "calling tool",
            "tool_calls": [{"id": "call_1", "type": "function"}],
            "token_count": 4,
        },
        {
            "role": "tool",
            "content": "tool result",
            "tool_call_id": "call_1",
            "token_count": 4,
        },
        {"role": "user", "content": "q2", "token_count": 3},
        {"role": "assistant", "content": "answer", "token_count": 3},
    ]
    request = CompactionRequest(
        session_id="boundary-start-test",
        entries=entries,
        context_window_tokens=23,
        config=synthetic_compaction_config(safety_margin=1.0),
    )

    result = await compact_context_new(request)

    assert result.removed_count == 0
    assert result.summary_source == "llm"
    assert result.kept_entries == entries
    assert result.skip_reason == "summary_does_not_fit"
    assert result.quality_report["fits_context_window"] is False


@pytest.mark.asyncio
async def test_new_prev_summary_marker_remains_backward_compatible():
    """The legacy marker is consumed as rolling context, not as instructions."""
    entries = [
        {"role": "user", "content": "a " * 500, "token_count": 500},
        {"role": "assistant", "content": "b " * 500, "token_count": 500},
        {"role": "user", "content": "Continue.", "token_count": 5},
        {"role": "assistant", "content": "Continuing.", "token_count": 5},
    ]
    config = synthetic_compaction_config(
        summary="prior context here; earlier work completed.", safety_margin=1.0,
    )
    request = CompactionRequest(
        session_id="prev-summary-test", entries=entries, context_window_tokens=500,
        config=config,
        custom_instructions="__prev_summary__:prior context here\nnormal instructions",
    )
    result = await compact_context_new(request)

    assert result.removed_count == 2
    assert result.summary
    assert "prior context here" in result.summary
    assert "__prev_summary__:" not in result.summary
    assert config.llm_plan is not None
    [(messages, _, chat_config)] = config.llm_plan.primary.provider.calls
    assert "[Existing portable checkpoint to replace]\nprior context here" in messages[0].content
    assert "normal instructions" in messages[0].content
    assert "__prev_summary__:" not in messages[0].content + chat_config.system


@pytest.mark.asyncio
async def test_new_returns_skipped_when_within_budget():
    """No compaction when tokens fit comfortably in the window."""
    entries = [
        {"role": "user", "content": "hi", "token_count": 5},
        {"role": "assistant", "content": "hello", "token_count": 5},
    ]
    request = CompactionRequest(
        session_id="budget-test",
        entries=entries,
        context_window_tokens=8192,
    )
    result = await compact_context_new(request)
    assert result.removed_count == 0
    assert result.summary_source == "skipped"
    assert result.kept_entries == entries
    assert result.skip_reason == "within_compaction_budget"


@pytest.mark.asyncio
async def test_new_empty_entries():
    """Empty entries list returns a no-op skipped result."""
    request = CompactionRequest(
        session_id="empty-test",
        entries=[],
        context_window_tokens=8192,
    )
    result = await compact_context_new(request)
    assert result.removed_count == 0
    assert result.kept_entries == []
    assert result.summary == ""
    assert result.skip_reason == "no_entries"
