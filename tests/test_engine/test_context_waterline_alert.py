"""Tests for the advisory context waterline alert (P1).

Covers:
- Alert emitted exactly once when durable history crosses the waterline ratio
- No duplicate alerts while latched
- Latch cleared on pressure turns (compaction takes over) so a later
  re-crossing can alert again
- Latch cleared and no alert when pressure drops back under the line
- No alert once automatic preflight compaction takes over
- Advisory failure never breaks the turn
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.session.models import TranscriptEntry


def _make_entry(content: str, role: str = "user") -> TranscriptEntry:
    return TranscriptEntry(
        session_id="test-session-id",
        session_key="user:session",
        role=role,
        content=content,
    )


class _RecordingSessionManager:
    def __init__(self, transcript: list[TranscriptEntry]) -> None:
        self._transcript = transcript
        self.append_message_calls: list[dict[str, Any]] = []
        self.compact_with_result_calls: list[tuple[Any, ...]] = []

    async def get_transcript(self, session_key: str) -> list[TranscriptEntry]:
        return list(self._transcript)

    async def append_message(
        self, session_key: str, **kwargs: Any
    ) -> None:
        self.append_message_calls.append(dict(kwargs))

    async def compact_with_result(
        self,
        session_key: str,
        context_window_tokens: int,
        config: object | None = None,
        **kwargs: Any,
    ) -> SimpleNamespace:
        self.compact_with_result_calls.append(
            (session_key, context_window_tokens, config)
        )
        return SimpleNamespace(
            summary="summary text",
            kept_entries=[{"role": "assistant", "content": "tail"}],
            removed_count=2,
            chunks_processed=1,
            summary_source="llm",
            tokens_before=1000,
            tokens_after=200,
            remaining_budget_tokens=800,
        )


def _runner(sm: Any) -> TurnRunner:
    return TurnRunner(provider_selector=MagicMock(), session_manager=sm)


def _alert_contents(sm: _RecordingSessionManager) -> list[str]:
    return [
        str(call.get("content"))
        for call in sm.append_message_calls
        if call.get("role") == "system"
    ]


@pytest.mark.asyncio
async def test_waterline_alert_emitted_once_per_crossing() -> None:
    """First crossing emits one system alert; repeat turns stay silent."""

    transcript = [_make_entry("old user"), _make_entry("old assistant", role="assistant")]
    for entry in transcript:
        entry.token_count = 400

    sm = _RecordingSessionManager(transcript)
    runner = _runner(sm)

    # Window 1000, used 800 -> 80%: between the 70% waterline and 85% auto line.
    await runner._maybe_preflight_compact("user:session", 1000)
    await runner._maybe_preflight_compact("user:session", 1000)

    alerts = _alert_contents(sm)
    assert len(alerts) == 1
    assert "80%" in alerts[0]
    assert "/compact" in alerts[0]
    assert "user:session" in runner._context_waterline_alerted_sessions


@pytest.mark.asyncio
async def test_waterline_full_lifecycle_alert_compaction_realert() -> None:
    """Cross -> alerted; growth into auto-compaction -> latch cleared, silent;
    post-compaction re-crossing -> alerts again."""

    transcript = [_make_entry("old user"), _make_entry("old assistant", role="assistant")]
    for entry in transcript:
        entry.token_count = 400

    sm = _RecordingSessionManager(transcript)
    runner = _runner(sm)

    await runner._maybe_preflight_compact("user:session", 1000)
    assert len(_alert_contents(sm)) == 1

    # Growth past the auto line (900 > 850): compaction path must clear the
    # latch and must not emit an advisory alert.
    for entry in sm._transcript:
        entry.token_count = 450
    await runner._maybe_preflight_compact("user:session", 1000)
    assert len(_alert_contents(sm)) == 1
    assert "user:session" not in runner._context_waterline_alerted_sessions

    # Post-compaction regrowth re-crosses the waterline: alert again.
    for entry in sm._transcript:
        entry.token_count = 400
    runner.clear_compaction_turn_state("user:session")  # simulate a new turn
    await runner._maybe_preflight_compact("user:session", 1000)
    assert len(_alert_contents(sm)) == 2


@pytest.mark.asyncio
async def test_waterline_no_alert_under_the_line_and_latch_cleared() -> None:
    """Below the waterline nothing is emitted; a stale latch is cleared."""

    transcript = [_make_entry("old user"), _make_entry("old assistant", role="assistant")]
    for entry in transcript:
        entry.token_count = 300

    sm = _RecordingSessionManager(transcript)
    runner = _runner(sm)
    runner._context_waterline_alerted_sessions.add("user:session")

    # Used 600 -> 60%: below the 70% line.
    await runner._maybe_preflight_compact("user:session", 1000)

    assert _alert_contents(sm) == []
    assert "user:session" not in runner._context_waterline_alerted_sessions


@pytest.mark.asyncio
async def test_waterline_no_alert_when_auto_compaction_takes_over() -> None:
    """A fresh crossing above the auto line never emits an advisory."""

    transcript = [_make_entry("old user"), _make_entry("old assistant", role="assistant")]
    for entry in transcript:
        entry.token_count = 450

    sm = _RecordingSessionManager(transcript)
    runner = _runner(sm)

    # Used 900 -> 90%: above the 85% auto line from a cold start.
    await runner._maybe_preflight_compact("user:session", 1000)

    assert _alert_contents(sm) == []


@pytest.mark.asyncio
async def test_waterline_alert_used_excludes_second_checkpoint_add() -> None:
    """Exact-value regression for the double-count fix.

    ``durable_history_tokens`` already contains the checkpoint payload
    (callers sum checkpoint + transcript entries). At 750/1000 the alert
    must fire at 75%. Under the previous double-add (used = 750 + 500 =
    1250 >= the 850 auto line) the helper went silent -- which is exactly
    the reviewer's inflated-accounting symptom, just past the auto line.
    """

    sm = _RecordingSessionManager([])
    runner = _runner(sm)

    await runner._emit_context_waterline_alert(
        "user:session",
        durable_history_tokens=750,
        checkpoint_tokens=500,
        history_window_tokens=1000,
    )

    alerts = _alert_contents(sm)
    assert len(alerts) == 1
    assert "75%" in alerts[0]


@pytest.mark.asyncio
async def test_waterline_alert_exact_numeric_boundaries() -> None:
    """Pin the emission band to exact token counts: silent below 700,
    alerting at 700..849, silent at the 850 auto line."""

    sm = _RecordingSessionManager([])
    runner = _runner(sm)

    async def used_for(durable: int) -> list[str]:
        runner._context_waterline_alerted_sessions.discard("user:session")
        sm.append_message_calls.clear()
        await runner._emit_context_waterline_alert(
            "user:session",
            durable_history_tokens=durable,
            checkpoint_tokens=0,
            history_window_tokens=1000,
        )
        return _alert_contents(sm)

    assert await used_for(699) == []
    assert len(await used_for(700)) == 1
    # 849 still fires (85%); 850 == the auto line stays silent.
    assert "85%" in (await used_for(849))[0]
    assert await used_for(850) == []


@pytest.mark.asyncio
async def test_waterline_alert_failure_never_breaks_the_turn() -> None:
    """A raising session manager must not propagate out of preflight."""

    transcript = [_make_entry("old user"), _make_entry("old assistant", role="assistant")]
    for entry in transcript:
        entry.token_count = 400

    sm = _RecordingSessionManager(transcript)

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("storage down")

    sm.append_message = _boom  # type: ignore[method-assign]
    runner = _runner(sm)

    # Must not raise.
    await runner._maybe_preflight_compact("user:session", 1000)
