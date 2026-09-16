"""Transport-neutral classification for unapplied compaction candidates."""

from typing import Final

STALE_COMPACTION_REASONS: Final[frozenset[str]] = frozenset(
    {
        "stale_preimage",
        "stale_context_state",
        "consumer_admission_stale",
        # Accept old adapters during a rolling upgrade without classifying them
        # as provider failures. New producers report the specific cause.
        "consumer_admission_stale_or_failed",
    }
)
BENIGN_AUTOMATIC_COMPACTION_SKIP_REASONS: Final[frozenset[str]] = frozenset(
    {
        "already_attempted_this_turn",
        "already_compacted_this_turn",
        "no_entries",
        "stale_preimage",
        "structured_content_noop",
        "within_budget",
        "within_compaction_budget",
    }
)


def compaction_failure_status(reason: str) -> str:
    """Classify an unapplied candidate consistently across automatic/manual callers."""

    if reason in STALE_COMPACTION_REASONS:
        return "stale"
    if reason in BENIGN_AUTOMATIC_COMPACTION_SKIP_REASONS or reason in {
        "no_safe_turn_boundary",
        "protected_tail_exhausts_compaction_window",
        "protected_boundary_missing",
        "current_request_only",
        "no_compactable_entries",
        "no_progress",
    }:
        return "skipped"
    return "failed"
