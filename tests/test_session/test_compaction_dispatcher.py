"""Tests for the compaction compaction public entry point.

: the dispatcher env-var, legacy implementation, and shadow comparator
have been deleted. ``compact_context`` delegates directly to
``compact_context_new``. These tests cover the public contract and the
turn-boundary cut behavior introduced in compaction.
"""

from __future__ import annotations

import pytest

from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    CompactionResult,
    compact_context,
    compact_context_new,
)


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
    # Build entries that would be split mid-turn by a pure token-budget cut.
    # Window = 30 tokens; keep_budget = 15.
    # Entries: u=5 a_tool=5 tool_result=5 u=5 a=5  (total=25)
    # A raw token-budget cut from the end would try to keep 15 tokens:
    #   a_final(5) + u_2(5) = 10 < 15, then tool_result(5) = 15 — keep 3 entries
    #   last_removed = a_tool → mid_turn!
    # The new impl should walk back one more step so last_removed = u_1 (turn_boundary).
    entries = [
        {"role": "user", "content": "q1", "token_count": 5},
        {
            "role": "assistant",
            "content": "[tool_call:read_file({\"path\": \"x\"})]",
            "token_count": 5,
        },
        {"role": "tool", "content": "[tool_result:read_file] contents", "token_count": 5},
        {"role": "user", "content": "q2", "token_count": 5},
        {"role": "assistant", "content": "answer", "token_count": 5},
    ]
    # Use a small window to force compaction; no API key so fallback summary is used.
    request = CompactionRequest(
        session_id="boundary-test",
        entries=entries,
        context_window_tokens=22,  # total=25 > 22*safety(1.2)=26.4 — actually under; use tighter
        config=CompactionConfig(safety_margin=1.0),
    )
    result = await compact_context_new(request)

    if result.removed_count > 0:
        # The cut should NOT land with last_removed = assistant tool_call
        # and first_kept = tool_result.
        removed = entries[: len(entries) - len(result.kept_entries)]
        kept = result.kept_entries
        if removed and kept:
            last_removed_role = removed[-1].get("role")
            last_removed_content = str(removed[-1].get("content") or "")
            first_kept_role = kept[0].get("role")
            is_mid_turn = (
                last_removed_role == "assistant"
                and "[tool_call:" in last_removed_content
                and first_kept_role == "tool"
            )
            assert not is_mid_turn, (
                f"Cut landed mid-turn: last_removed={removed[-1]}, first_kept={kept[0]}"
            )


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
        config=CompactionConfig(safety_margin=1.0),
    )
    result = await compact_context_new(request)

    assert result.removed_count > 0
    removed = entries[: len(entries) - len(result.kept_entries)]
    kept = result.kept_entries
    assert removed[-1]["content"] != "[Used tool: read_file]"
    assert kept[0]["content"] == "q1"
    assert kept[1]["content"] == "[Used tool: read_file]"


@pytest.mark.asyncio
async def test_new_skips_when_only_cut_would_orphan_tool_result():
    """If no clean boundary exists, compaction must not split tool state."""
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
        context_window_tokens=26,
        config=CompactionConfig(safety_margin=1.0),
    )

    result = await compact_context_new(request)

    assert result.removed_count == 0
    assert result.summary_source == "skipped"
    assert result.kept_entries == entries
    assert result.skip_reason == "no_safe_turn_boundary"


@pytest.mark.asyncio
async def test_new_prev_summary_marker_remains_backward_compatible():
    """The legacy marker is consumed as rolling context, not as instructions."""
    entries = [
        {"role": "user", "content": "a " * 500, "token_count": 500},
        {"role": "assistant", "content": "b " * 500, "token_count": 500},
    ]
    request = CompactionRequest(
        session_id="prev-summary-test",
        entries=entries,
        context_window_tokens=100,
        config=CompactionConfig(safety_margin=1.0),
        custom_instructions="__prev_summary__:prior context here\nnormal instructions",
    )
    result = await compact_context_new(request)
    if result.removed_count > 0:
        assert "prior context here" in result.summary
        assert "__prev_summary__:" not in result.summary


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
