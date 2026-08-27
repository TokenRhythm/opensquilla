"""Regression tests for the degraded-compaction data-loss chain.

Covers the guards added after the webchat:f4d2b4dc incident (two
budget-guarded compaction LLM failures, then a deterministic fallback that
committed a ~68-token stub over a ~170K structured checkpoint):

- inflated persisted ``token_count`` must be capped at the entry's own
  serialized size (read-side accounting cap);
- a pure deterministic fallback must refuse to commit when a substantive
  prior checkpoint summary exists;
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
# B2: read-side cap on inflated persisted token_count
# ---------------------------------------------------------------------------


def test_inflated_persisted_token_count_capped_to_serialized_size() -> None:
    tool_calls = [
        {"id": f"call-{i}", "name": "exec_command", "input": "x" * 2_000}
        for i in range(40)
    ]
    entry = {
        "role": "assistant",
        "content": "",
        "token_count": 770_000,
        "tool_calls": tool_calls,
    }

    estimated = estimate_entry_model_replay_tokens(entry)

    # The provably inflated book value (whole-turn cumulative output) must
    # no longer dominate the entry's replay size.
    assert estimated < 200_000
    # ... but the entry's own serialized payload must still be fully counted.
    assert estimated >= 80_000


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


@pytest.mark.asyncio
async def test_fallback_refuses_to_destroy_rich_prior_summary(monkeypatch) -> None:
    async def failing_llm(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "opensquilla.session.compaction.call_compaction_llm", failing_llm
    )
    rich_summary = "[Structured Compaction Summary]\n" + "decision fact. " * 200

    result = await compact_context(
        CompactionRequest(
            session_id="fallback-degraded",
            entries=_make_entries(20, tokens_each=50),
            context_window_tokens=500,
            config=_degraded_config(),
            previous_summary=rich_summary,
        )
    )

    assert result.summary_source == "fallback"
    assert result.removed_count == 0
    assert result.kept_entries  # nothing was destroyed
    assert result.skip_reason == "fallback_degraded_with_prior_summary"
    assert result.tokens_after == result.tokens_before


@pytest.mark.asyncio
async def test_fallback_stub_prior_summary_is_replaceable(monkeypatch) -> None:
    """A stub checkpoint carries no state worth protecting."""

    async def failing_llm(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "opensquilla.session.compaction.call_compaction_llm", failing_llm
    )

    result = await compact_context(
        CompactionRequest(
            session_id="fallback-stub-prev",
            entries=_make_entries(20, tokens_each=50),
            context_window_tokens=500,
            config=_degraded_config(),
            previous_summary="tiny stub",
        )
    )

    assert result.skip_reason != "fallback_degraded_with_prior_summary"


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
