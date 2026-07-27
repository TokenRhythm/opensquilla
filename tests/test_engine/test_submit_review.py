"""Unit tests for the pure review-on-submit state machine.

The state machine here is I/O-free and owns the entire fire/skip decision for
both the explicit ``submit`` tool and the implicit finalize checkpoint. The
agent loop only captures the diff and threads the results through; these tests
pin the exact transition semantics the loop relies on, plus the forbidden-token
contract on every authored runtime string.
"""

from __future__ import annotations

import pytest

from opensquilla.engine.submit_review import (
    SUBMIT_REVIEW_NUDGE_LIMIT,
    SubmitAction,
    SubmitReviewState,
    assert_runtime_strings_clean,
    build_submit_review_message,
    confirmation_message,
    diff_is_truncated,
    empty_diff_note,
    evaluate_explicit_submit,
    nudge_message,
    observe_tool_activity,
    should_fire_implicit,
)

_FORBIDDEN = ("minimal", "localized", "not sufficient")


# ---------------------------------------------------------------------------
# evaluate_explicit_submit
# ---------------------------------------------------------------------------


def test_explicit_empty_diff_does_not_consume_review() -> None:
    state = SubmitReviewState()
    action = evaluate_explicit_submit(state, diff_empty=True, headroom_ok=True)
    assert action is SubmitAction.EMPTY_DIFF_NOTE
    # A premature submit must leave the one review unspent.
    assert state.stage == 0
    assert state.reviewed_via is None


def test_explicit_first_submit_shows_checklist_and_advances() -> None:
    state = SubmitReviewState()
    action = evaluate_explicit_submit(state, diff_empty=False, headroom_ok=True)
    assert action is SubmitAction.SHOW_CHECKLIST
    assert state.stage == 1
    assert state.reviewed_via == "explicit"
    assert state.acted_since_review is False


def test_explicit_first_submit_without_headroom_confirms_immediately() -> None:
    state = SubmitReviewState()
    action = evaluate_explicit_submit(state, diff_empty=False, headroom_ok=False)
    # No budget for a follow-up call: never strand the submission behind a review.
    assert action is SubmitAction.CONFIRM
    assert state.stage == 2


def test_explicit_resubmit_without_work_nudges_once() -> None:
    state = SubmitReviewState()
    evaluate_explicit_submit(state, diff_empty=False, headroom_ok=True)  # checklist
    action = evaluate_explicit_submit(state, diff_empty=False, headroom_ok=True)
    assert action is SubmitAction.NUDGE
    assert state.nudges == 1
    assert state.stage == 1


def test_explicit_nudge_is_capped_then_confirms() -> None:
    state = SubmitReviewState()
    evaluate_explicit_submit(state, diff_empty=False, headroom_ok=True)  # checklist
    for _ in range(SUBMIT_REVIEW_NUDGE_LIMIT):
        assert (
            evaluate_explicit_submit(state, diff_empty=False, headroom_ok=True)
            is SubmitAction.NUDGE
        )
    # Nudge budget spent: the next rubber-stamp resubmit is accepted.
    action = evaluate_explicit_submit(state, diff_empty=False, headroom_ok=True)
    assert action is SubmitAction.CONFIRM
    assert state.stage == 2


def test_explicit_resubmit_after_real_work_confirms() -> None:
    state = SubmitReviewState()
    evaluate_explicit_submit(state, diff_empty=False, headroom_ok=True)  # checklist
    observe_tool_activity(state)  # the model kept working after the review
    action = evaluate_explicit_submit(state, diff_empty=False, headroom_ok=True)
    assert action is SubmitAction.CONFIRM
    assert state.stage == 2
    assert state.nudges == 0


def test_confirmed_state_stays_confirmed() -> None:
    state = SubmitReviewState()
    evaluate_explicit_submit(state, diff_empty=False, headroom_ok=True)
    observe_tool_activity(state)
    evaluate_explicit_submit(state, diff_empty=False, headroom_ok=True)  # confirm
    action = evaluate_explicit_submit(state, diff_empty=False, headroom_ok=True)
    assert action is SubmitAction.CONFIRM
    assert state.stage == 2


# ---------------------------------------------------------------------------
# observe_tool_activity / mark_reviewed
# ---------------------------------------------------------------------------


def test_observe_tool_activity_is_noop_before_review() -> None:
    state = SubmitReviewState()
    observe_tool_activity(state)
    assert state.acted_since_review is False


def test_observe_tool_activity_marks_after_review() -> None:
    state = SubmitReviewState()
    state.mark_reviewed("explicit")
    observe_tool_activity(state)
    assert state.acted_since_review is True


def test_mark_reviewed_is_idempotent() -> None:
    state = SubmitReviewState()
    state.mark_reviewed("explicit")
    state.mark_reviewed("implicit")  # must not overwrite or reset
    assert state.stage == 1
    assert state.reviewed_via == "explicit"


# ---------------------------------------------------------------------------
# should_fire_implicit
# ---------------------------------------------------------------------------


def _fire_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        enabled=True,
        diff_empty=False,
        headroom_ok=True,
        other_gate_injected=False,
        red_detected=False,
        pending_flags_clear=True,
    )
    base.update(overrides)
    return base


def test_implicit_fires_for_unreviewed_green_diff() -> None:
    state = SubmitReviewState()
    assert should_fire_implicit(state, **_fire_kwargs()) is True


@pytest.mark.parametrize(
    "override",
    [
        {"enabled": False},
        {"diff_empty": True},
        {"headroom_ok": False},
        {"other_gate_injected": True},
        {"red_detected": True},
        {"pending_flags_clear": False},
    ],
)
def test_implicit_excluded_by_each_guard(override: dict[str, object]) -> None:
    state = SubmitReviewState()
    assert should_fire_implicit(state, **_fire_kwargs(**override)) is False


def test_implicit_does_not_fire_after_review() -> None:
    state = SubmitReviewState()
    state.mark_reviewed("explicit")
    assert should_fire_implicit(state, **_fire_kwargs()) is False


# ---------------------------------------------------------------------------
# diff truncation
# ---------------------------------------------------------------------------


def test_diff_is_truncated_threshold() -> None:
    assert diff_is_truncated("x" * 100, 200) is False
    assert diff_is_truncated("x" * 300, 200) is True
    # A zero cap disables truncation entirely.
    assert diff_is_truncated("x" * 10_000, 0) is False


def test_long_diff_keeps_head_and_tail() -> None:
    diff = "HEADMARK" + ("m" * 5000) + "TAILMARK"
    msg = build_submit_review_message("file.py | 2 +-", diff, implicit=False, max_chars=400)
    assert "HEADMARK" in msg
    assert "TAILMARK" in msg
    assert "diff truncated" in msg
    # The per-file summary is never clipped, even when the body is.
    assert "file.py | 2 +-" in msg


def test_short_diff_is_shown_whole() -> None:
    diff = "diff --git a/x b/x\n+one line\n"
    msg = build_submit_review_message("x | 1 +", diff, implicit=False, max_chars=20000)
    assert diff in msg
    assert "diff truncated" not in msg


# ---------------------------------------------------------------------------
# message rendering
# ---------------------------------------------------------------------------


def test_checklist_covers_general_hygiene_items() -> None:
    msg = build_submit_review_message("f | 1 +", "diff-body", implicit=False)
    lowered = msg.lower()
    assert "other places" in lowered  # parallel call-sites
    assert "revert" in lowered  # debug-only file cleanup
    assert "verification" in lowered  # re-run after late edits
    assert "scratch" in lowered  # delete temp files
    assert "call submit to confirm" in lowered


def test_implicit_message_warns_about_auto_submit() -> None:
    explicit = build_submit_review_message("f | 1 +", "d", implicit=False)
    implicit = build_submit_review_message("f | 1 +", "d", implicit=True)
    assert "will be submitted as they are" in implicit
    assert "will be submitted as they are" not in explicit


def test_empty_index_has_placeholder() -> None:
    msg = build_submit_review_message("", "some-diff", implicit=False)
    assert "no per-file summary available" in msg


# ---------------------------------------------------------------------------
# forbidden-token contract
# ---------------------------------------------------------------------------


def test_runtime_strings_have_no_forbidden_tokens() -> None:
    for text in (
        empty_diff_note(),
        nudge_message(),
        confirmation_message(),
        build_submit_review_message("f | 1 +", "body", implicit=False),
        build_submit_review_message("f | 1 +", "body", implicit=True),
    ):
        lowered = text.lower()
        for token in _FORBIDDEN:
            assert token not in lowered


def test_assert_runtime_strings_clean_passes() -> None:
    # Must not raise: the module's own self-check over every authored template.
    assert_runtime_strings_clean()
