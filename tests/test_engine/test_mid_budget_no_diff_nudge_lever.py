"""Read-side compatibility for retired progress and endgame nudge history."""

from __future__ import annotations

import pytest

from opensquilla.engine.agent import _is_runtime_nudge_message
from opensquilla.provider import Message

_HISTORICAL_MID_BUDGET_NUDGE = (
    "Progress check: about 55% of the wall-clock budget for this task "
    "is spent and the workspace has no source change yet. If you already "
    "know the fix, start implementing it now and verify it against the "
    "existing tests. If you are still investigating, pick the most likely "
    "file and make the smallest reasonable edit now, then refine it with the "
    "remaining time instead of leaving the whole budget to analysis."
)
_HISTORICAL_ENDGAME_FIX_DIRECTIVE = (
    "Time check: about 2 minute(s) remain and the workspace contains "
    "no source fix yet beyond diagnostic instrumentation. Stop investigating "
    "now. Decide on the most likely root cause from the evidence you already "
    "have, remove leftover debug output, apply your best-supported fix to "
    "the source code, and verify it directly. An imperfect fix you can "
    "defend beats no fix."
)


def test_tail_shape_helper_skips_nudges_only() -> None:
    from opensquilla.engine.agent import (
        _tail_has_tool_result_ignoring_nudges,
    )
    from opensquilla.provider import ContentBlockToolResult

    tool_results = Message(
        role="user",
        content=[
            ContentBlockToolResult(tool_use_id="use-1", content="tool ok"),
        ],
    )
    nudge = Message(
        role="user",
        content=_HISTORICAL_MID_BUDGET_NUDGE,
    )
    guidance = Message(role="user", content="[Progress warning] no forward progress")

    assert _tail_has_tool_result_ignoring_nudges([tool_results, guidance, nudge])
    assert _tail_has_tool_result_ignoring_nudges([tool_results, nudge, guidance])
    # Without a tool result in the tail the shape stays non-post-tool: the
    # helper only removes nudges, it never widens what counts as a tool turn.
    assert not _tail_has_tool_result_ignoring_nudges(
        [Message(role="user", content="question"), guidance, nudge]
    )
    assert not _tail_has_tool_result_ignoring_nudges([nudge])
    assert not _tail_has_tool_result_ignoring_nudges([])
    assert not _tail_has_tool_result_ignoring_nudges(
        [
            tool_results,
            guidance,
            Message(
                role="user",
                content="Progress check: about the earlier result, please explain it.",
            ),
        ]
    )


@pytest.mark.parametrize(
    "content",
    [
        _HISTORICAL_MID_BUDGET_NUDGE,
        _HISTORICAL_MID_BUDGET_NUDGE.replace("55%", "75%"),
        _HISTORICAL_ENDGAME_FIX_DIRECTIVE,
        _HISTORICAL_ENDGAME_FIX_DIRECTIVE.replace("2 minute(s)", "12 minute(s)"),
    ],
)
def test_complete_historical_nudge_is_recognized(content: str) -> None:
    assert _is_runtime_nudge_message(Message(role="user", content=content))
    assert not _is_runtime_nudge_message(Message(role="assistant", content=content))
    assert not _is_runtime_nudge_message(Message(role="user", content="Continue the task."))


@pytest.mark.parametrize(
    "content",
    [
        "Progress check: about the earlier result, please explain it.",
        "Time check: about when will the task finish?",
        "Progress check: about 55% of the wall-clock budget for this task is spent.",
        "Time check: about 2 minute(s) remain and the workspace contains no source fix.",
        _HISTORICAL_MID_BUDGET_NUDGE.replace("55%", "many%"),
        _HISTORICAL_ENDGAME_FIX_DIRECTIVE.replace("2 minute(s)", "two minute(s)"),
        _HISTORICAL_MID_BUDGET_NUDGE + " Please explain this advice.",
        _HISTORICAL_ENDGAME_FIX_DIRECTIVE + "\nIs this still relevant?",
    ],
)
def test_same_prefix_user_text_is_not_a_historical_nudge(content: str) -> None:
    assert not _is_runtime_nudge_message(Message(role="user", content=content))
