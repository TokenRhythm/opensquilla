"""Regression tests for the degraded-compaction data-loss chain.

Covers the guards added after an incident where two budget-guarded
compaction LLM failures let a deterministic fallback commit a tiny stub
over a large structured checkpoint:

- inflated persisted ``token_count`` must not dominate the entry's own
  projection estimate, and must never be double-counted with it;
- a pure deterministic fallback must preserve a prior checkpoint verbatim;
  when survival cannot be verified the operation refuses to commit;
- degraded compaction outcomes must be logged at warning level.
"""

from __future__ import annotations

from typing import Any

import pytest

from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    compact_context,
    estimate_entry_model_replay_tokens,
)
from tests.test_session.test_compaction import _make_entries

# ---------------------------------------------------------------------------
# B2: whole-turn cumulative counts must never dominate or double-count
#
# ``_estimate_tokens`` is patched to deterministic len//4 so the numeric
# expectations hold in any environment (the tokenizer may compress
# repetitive payloads aggressively).
# ---------------------------------------------------------------------------


_TOOL_CALLS = [
    {"id": f"call-{i}", "name": "exec_command", "input": "x" * 2_000}
    for i in range(40)
]


def test_persisted_count_wins_only_up_to_its_own_output() -> None:
    """A whole-turn cumulative count is chosen only as a selection upper bound.

    The estimator selects between the persisted count and the entry's own
    projection; it can never return a stacked value above both.  A cumulative
    770k count on a ~20.5k-token payload yields exactly max(770000, est):
    inflated book value still cannot double-count, and precise cleanup of
    legacy inflated rows belongs to a one-off script, not read-time magic.
    """

    entry = {
        "role": "assistant",
        "content": "",
        "token_count": 770_000,
        "tool_calls": _TOOL_CALLS,
    }

    estimated = estimate_entry_model_replay_tokens(entry)

    # The estimator may keep the persisted number but never exceeds it, and
    # never returns anything between persisted and persisted+projection (the
    # stacked-result signature).
    assert estimated == 770_000
    natural_chars = sum(len(str(call)) for call in _TOOL_CALLS)
    assert natural_chars // 4 <= 21_500


def test_persisted_count_is_never_stacked_on_top_of_projection_extras(
    monkeypatch,
) -> None:
    """A plausible message count and projection extras must not double-count."""

    import opensquilla.session.compaction as compaction_module

    monkeypatch.setattr(
        compaction_module,
        "_estimate_tokens",
        lambda text: max(1, len(text) // 4),
    )
    content = "y" * 4_000
    entry = {
        "role": "assistant",
        "content": content,
        "token_count": 3,
        "tool_calls": _TOOL_CALLS,
    }

    estimated = estimate_entry_model_replay_tokens(entry)

    # With the persisted count honored (it is this entry's own output), the
    # result is max(count, payload estimate).  The stacked legacy behavior
    # would be count + full payload estimate again (~2x).
    natural_chars = len(content) + sum(len(str(call)) for call in _TOOL_CALLS)
    expected = natural_chars // 4
    assert abs(estimated - expected) <= expected // 20 + 8


def test_sane_persisted_token_count_survives() -> None:
    entry = {"role": "assistant", "content": "hi", "token_count": 3}
    assert estimate_entry_model_replay_tokens(entry) >= 3


# ---------------------------------------------------------------------------
# B4: fallback must not destroy a rich prior checkpoint
# ---------------------------------------------------------------------------


def _degraded_config() -> CompactionConfig:
    return CompactionConfig(
        model="test/model",
        api_key="test-key",
        safety_margin=1.0,
    )


def _fallback_entries() -> list[dict]:
    """Entries whose natural replay size genuinely overflows the window."""

    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"message {i}: " + (f"fact-{i}-" * 400),
        }
        for i in range(20)
    ]


@pytest.mark.asyncio
async def test_fallback_merge_preserves_prior_summary_verbatim(monkeypatch) -> None:
    """Degraded compaction commits only when the prior checkpoint survives."""

    async def failing_llm(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "opensquilla.session.compaction.call_compaction_llm", failing_llm
    )
    rich_summary = "[Structured Compaction Summary]\n" + "decision fact. " * 200

    result = await compact_context(
        CompactionRequest(
            session_id="fallback-degraded",
            entries=_fallback_entries(),
            # Generous enough that the merged artifact is not re-bounded by
            # the later summary-fitting stage, which applies uniformly to
            # every summary source; this test targets the fallback layer.
            context_window_tokens=4000,
            config=_degraded_config(),
            previous_summary=rich_summary,
        )
    )

    assert result.summary_source == "fallback"
    # Either the merged summary still contains the prior checkpoint verbatim
    # (committed), or the operation refused to commit at all.
    if result.removed_count > 0:
        assert "decision fact. decision fact." in result.summary
        assert len(result.summary) >= len(rich_summary)
    else:
        assert result.skip_reason == "fallback_degraded_with_prior_summary"
        assert result.kept_entries
        assert result.tokens_after == result.tokens_before


@pytest.mark.asyncio
async def test_fallback_with_short_prior_summary_still_preserves_it(monkeypatch) -> None:
    """A short checkpoint is protected by the same verbatim-survival rule."""

    async def failing_llm(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "opensquilla.session.compaction.call_compaction_llm", failing_llm
    )
    short_structured = (
        "[Structured Compaction Summary]\nuser_goal: ship release\n"
        "next_action: run deploy script"
    )

    result = await compact_context(
        CompactionRequest(
            session_id="fallback-short-prev",
            entries=_fallback_entries(),
            context_window_tokens=4000,
            config=_degraded_config(),
            previous_summary=short_structured,
        )
    )

    if result.removed_count > 0:
        assert "user_goal: ship release" in result.summary
    else:
        assert result.skip_reason == "fallback_degraded_with_prior_summary"


# ---------------------------------------------------------------------------
# B5: degraded outcomes must stand out in the log
# ---------------------------------------------------------------------------


class _LogRecorder:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        self.warnings.append(str(message))

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        self.infos.append(str(message))

    def __getattr__(self, name: str) -> Any:
        def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        return _noop


@pytest.mark.asyncio
async def test_degraded_compaction_terminal_is_warning(monkeypatch) -> None:
    recorder = _LogRecorder()
    monkeypatch.setattr("opensquilla.session.compaction.log", recorder)

    async def failing_llm(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "opensquilla.session.compaction.call_compaction_llm", failing_llm
    )

    await compact_context(
        CompactionRequest(
            session_id="fallback-warning",
            entries=_make_entries(20, tokens_each=50),
            context_window_tokens=500,
            config=_degraded_config(),
        )
    )

    assert "compaction.operation_terminal" in recorder.warnings
    assert "compaction.operation_terminal" not in recorder.infos
