"""Regression tests for assistant-entry ``token_count`` accounting.

The finalizer must persist a token_count that describes the entry's own
replayable content.  A whole turn's cumulative output (across every retried
execution leg) must not be copied into the entry's token_count: it inflates
the persistent token ledger and makes preflight compaction trigger on book
value far above the real replay size (observed as a 770k count on an entry
whose replayable payload was ~22k tokens).
"""

from __future__ import annotations

import pytest

from opensquilla.engine.types import DoneEvent
from tests.test_engine.turn_runner.test_turn_finalizer_stage_unit import (
    _make_input,
    _make_stage,
)


@pytest.mark.asyncio
async def test_cumulative_multi_leg_output_does_not_inflate_token_count() -> None:
    """A whole turn's cumulative output must not become the entry's count."""

    stage, recs = _make_stage()
    done = DoneEvent(
        text="hi",
        input_tokens=7_580_647,
        output_tokens=770_000,
        model="synthetic-turn-model-4.5",
    )
    await stage.run(_make_input(final_text_parts=["hi"], done_event=done))

    recorded = recs["transcript_append"].calls[0]["token_count"]
    assert recorded is not None
    assert recorded != 770_000
    # The entry only replays "hi" (~1 token) plus nothing else.
    assert recorded <= 3


@pytest.mark.asyncio
async def test_single_leg_output_count_keeps_precision() -> None:
    """A plausible single-leg value is preserved (no precision loss)."""

    stage, recs = _make_stage()
    done = DoneEvent(text="ok", input_tokens=5, output_tokens=3)
    await stage.run(_make_input(final_text_parts=["ok"], done_event=done))

    assert recs["transcript_append"].calls[0]["token_count"] == 3


@pytest.mark.asyncio
async def test_message_level_count_still_wins() -> None:
    """message_output_tokens remains the authoritative source."""

    stage, recs = _make_stage()
    done = DoneEvent(text="ok", input_tokens=5, output_tokens=999)
    done.message_output_tokens = 42
    await stage.run(_make_input(final_text_parts=["ok"], done_event=done))

    assert recs["transcript_append"].calls[0]["token_count"] == 42
