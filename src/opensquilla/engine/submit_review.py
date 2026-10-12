"""Pure, I/O-free state machine for the review-on-submit finalize checkpoint.

This module owns no I/O. The agent loop owns one :class:`SubmitReviewState` per
run, captures the workspace diff, and calls these helpers to decide whether to
show the model a review of its own changes before its work is finalized.

Two entry points share one state:

* **explicit** — the model calls the ``submit`` tool (intercepted in the loop
  exactly like ``meta_invoke``); :func:`evaluate_explicit_submit` returns the
  action to take and mutates the state.
* **implicit** — the model stops emitting tool calls while holding a non-empty
  workspace diff; :func:`should_fire_implicit` decides whether the review is
  injected as a user message before the existing finalize chain lets the turn
  end. The implicit path is what guarantees the review reaches a run even when
  the model never adopts the tool.

The review fires at most once per run (one checklist), plus at most one
anti-rubber-stamp restatement, after which every finish intent passes through
unchanged. The single monotonic per-run ``stage`` — deliberately not keyed on
any hash of the diff or evidence state — makes the per-state re-fire storm that
a state-keyed gate can produce structurally impossible.

Every runtime string here is general coding hygiene: it names no test files, no
reproduction scripts, and no benchmark-specific artifact.
:func:`assert_runtime_strings_clean` enforces the forbidden-token contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# One checklist and one restatement per run, ever. Both bounds are hard.
SUBMIT_REVIEW_STAGE_LIMIT = 1
SUBMIT_REVIEW_NUDGE_LIMIT = 1
SUBMIT_REVIEW_DIFF_MAX_CHARS_DEFAULT = 20000

# Runtime strings shown to the model must never contain these tokens.
_FORBIDDEN_SUBSTRINGS = ("minimal", "localized", "not sufficient")

# Reserve room for the truncation marker when splitting a long diff head+tail.
_TRUNCATION_MARKER_RESERVE = 160


class SubmitAction(Enum):
    """Outcome of an explicit ``submit`` call."""

    EMPTY_DIFF_NOTE = "empty_diff_note"
    SHOW_CHECKLIST = "show_checklist"
    NUDGE = "nudge"
    CONFIRM = "confirm"


@dataclass
class SubmitReviewState:
    """Per-run review progress. Owned by the agent loop; mutated in place."""

    stage: int = 0  # 0 = unreviewed, 1 = reviewed, 2 = confirmed
    reviewed_via: str | None = None  # "explicit" | "implicit"
    nudges: int = 0
    acted_since_review: bool = False

    def mark_reviewed(self, via: str) -> None:
        """Advance to the reviewed stage exactly once."""
        if self.stage < 1:
            self.stage = 1
            self.reviewed_via = via
            self.acted_since_review = False


def observe_tool_activity(state: SubmitReviewState) -> None:
    """Record that the model executed a real (non-``submit``) tool.

    Only meaningful once a review has been shown; it distinguishes a model that
    kept working after seeing the checklist from one immediately re-submitting.
    """
    if state.stage >= 1:
        state.acted_since_review = True


def evaluate_explicit_submit(
    state: SubmitReviewState,
    *,
    diff_empty: bool,
    headroom_ok: bool,
) -> SubmitAction:
    """Decide what an explicit ``submit`` call should do, mutating ``state``."""
    if diff_empty:
        # A premature/exploratory submit must not consume the run's one review.
        return SubmitAction.EMPTY_DIFF_NOTE
    if state.stage == 0:
        if not headroom_ok:
            # No budget for a follow-up call: never strand the submission.
            state.stage = 2
            return SubmitAction.CONFIRM
        state.mark_reviewed("explicit")
        return SubmitAction.SHOW_CHECKLIST
    if (
        state.stage == 1
        and not state.acted_since_review
        and state.nudges < SUBMIT_REVIEW_NUDGE_LIMIT
        and headroom_ok
    ):
        state.nudges += 1
        return SubmitAction.NUDGE
    state.stage = 2
    return SubmitAction.CONFIRM


def should_fire_implicit(
    state: SubmitReviewState,
    *,
    enabled: bool,
    diff_empty: bool,
    headroom_ok: bool,
    other_gate_injected: bool,
    red_detected: bool,
    pending_flags_clear: bool,
) -> bool:
    """Whether the implicit finalize path should inject the review this attempt.

    Fires only for the never-reviewed, non-empty-diff, no-red-detected,
    no-other-gate-fired, headroom-available, no-forced-finalization population —
    exactly the finalize-on-green case the red-evidence gate is blind to.
    """
    return bool(
        enabled
        and state.stage == 0
        and not diff_empty
        and headroom_ok
        and not other_gate_injected
        and not red_detected
        and pending_flags_clear
    )


def diff_is_truncated(diff_text: str, max_chars: int) -> bool:
    """Whether :func:`build_submit_review_message` will truncate this diff."""
    return max_chars > 0 and len(diff_text) > max_chars


def _truncate_diff(diff_text: str, max_chars: int) -> tuple[str, bool]:
    """Head+tail truncation so both the first and last hunks stay visible."""
    total = len(diff_text)
    if not diff_is_truncated(diff_text, max_chars):
        return diff_text, False
    budget = max(0, max_chars - _TRUNCATION_MARKER_RESERVE)
    head = budget // 2
    tail = budget - head
    shown = head + tail
    marker = (
        f"[diff truncated: {shown} of {total} characters shown; the file list "
        "above is complete — run git_diff for the rest]"
    )
    head_txt = diff_text[:head]
    tail_txt = diff_text[total - tail :] if tail else ""
    return f"{head_txt}\n{marker}\n{tail_txt}", True


def build_submit_review_message(
    file_index: str,
    diff_text: str,
    *,
    implicit: bool,
    max_chars: int = SUBMIT_REVIEW_DIFF_MAX_CHARS_DEFAULT,
) -> str:
    """Render the review shown to the model (explicit result or injected user message).

    ``file_index`` (path + change counts + untracked entries) is always shown in
    full; only the diff body is truncated, so the actionable file list survives
    even if the body is clipped.
    """
    truncated_diff, _ = _truncate_diff(diff_text, max_chars)
    index = file_index.strip() or "(no per-file summary available)"
    lines = [
        "[Submit review]",
        (
            "Your current changes are shown below. Walk through this checklist "
            "before your work is finalized:"
        ),
        (
            "1. Search for other places in the codebase that use the code you "
            "changed — callers of the same function, copies of the same "
            "pattern, or related constants. If the same issue exists at another "
            "site, fix it there too."
        ),
        (
            "2. Review each change in the diff. Keep every change your fix "
            "needs. Revert files you only changed to help yourself debug rather "
            "than to solve the task (for example: git checkout -- <file>). Do "
            "not revert changes that are part of your fix."
        ),
        (
            "3. If you edited any code after your last verification run, run "
            "your verification command again now and confirm it passes."
        ),
        (
            "4. Delete temporary or scratch files you created while working "
            "(scratch scripts, log dumps, notes), unless the task asked for "
            "them."
        ),
        (
            "5. When every item checks out, call submit to confirm. Your "
            "workspace changes at that point become your final answer."
        ),
    ]
    if implicit:
        lines.append(
            "If you stop without calling submit after this review, your current "
            "changes will be submitted as they are."
        )
    lines.append("Per-file summary of your changes:")
    lines.append(index)
    lines.append("Full diff:")
    lines.append("<diff>")
    lines.append(truncated_diff)
    lines.append("</diff>")
    return "\n".join(lines)


def empty_diff_note() -> str:
    return (
        "You have no workspace changes yet. Continue working and call submit "
        "when you have changes to finalize."
    )


def nudge_message() -> str:
    return (
        "Before confirming, complete at least one checklist action — for "
        "example, run your verification command — or state in your final answer "
        "why no item applies. Then call submit again."
    )


def confirmation_message() -> str:
    return "Submission received; running final checks."


def _all_runtime_strings() -> list[str]:
    """Every template string shown to the model, with placeholder interpolants.

    The diff/file-index spans are placeholders (not real repo content), so the
    forbidden-token lint checks only the templates we author, never user code
    that may legitimately contain those tokens.
    """
    return [
        empty_diff_note(),
        nudge_message(),
        confirmation_message(),
        build_submit_review_message("", "", implicit=False),
        build_submit_review_message("", "", implicit=True),
    ]


def assert_runtime_strings_clean() -> None:
    """Raise if any authored runtime string carries a forbidden token."""
    for text in _all_runtime_strings():
        lowered = text.lower()
        for token in _FORBIDDEN_SUBSTRINGS:
            if token in lowered:
                raise AssertionError(
                    f"forbidden token {token!r} present in submit-review runtime string"
                )
