"""Pure post-compaction continuation decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

CompactionContinuationAction = Literal[
    "continue_after_compaction",
    "retry_after_compaction",
    "degraded_continue_after_compaction",
    "partial_after_compaction",
    "blocked_after_compaction",
    "failed_after_compaction",
]


@dataclass(frozen=True)
class CompactionContinuationDecision:
    action: CompactionContinuationAction
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_compaction_continuation(
    *,
    receipt_safe: bool,
    raw_session_durable: bool,
    semantic_flush_ok: bool,
    retry_count: int,
    max_retries: int,
    prompt_changed: bool,
    finalization_attempted: bool,
    context_unsalvageable: bool = False,
) -> CompactionContinuationDecision:
    """Decide whether compaction should continue, degrade, finalize, or block.

    The helper deliberately consumes booleans from existing receipt/flush checks
    instead of validating receipts itself, so raw/session substrate ownership stays
    with the session compaction lifecycle layer.
    """

    details = {
        "receipt_safe": receipt_safe,
        "raw_session_durable": raw_session_durable,
        "semantic_flush_ok": semantic_flush_ok,
        "retry_count": retry_count,
        "max_retries": max_retries,
        "prompt_changed": prompt_changed,
        "finalization_attempted": finalization_attempted,
        "context_unsalvageable": context_unsalvageable,
    }
    if context_unsalvageable or not receipt_safe:
        return CompactionContinuationDecision(
            "blocked_after_compaction",
            "context_unsalvageable",
            details,
        )
    if prompt_changed and semantic_flush_ok:
        return CompactionContinuationDecision(
            "continue_after_compaction",
            "receipt_safe_prompt_changed",
            details,
        )
    if retry_count < max_retries:
        return CompactionContinuationDecision(
            "retry_after_compaction",
            "prompt_not_reduced",
            details,
        )
    if raw_session_durable and not semantic_flush_ok:
        return CompactionContinuationDecision(
            "degraded_continue_after_compaction",
            "semantic_flush_degraded_raw_durable",
            details,
        )
    if finalization_attempted:
        return CompactionContinuationDecision(
            "failed_after_compaction",
            "finalization_failed_after_retries",
            details,
        )
    return CompactionContinuationDecision(
        "partial_after_compaction",
        "finalization_required_after_retries",
        details,
    )
