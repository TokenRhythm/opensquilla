"""Deterministic promotion ranking for Dream."""

from __future__ import annotations

import math

from opensquilla.memory.dream.models import (
    PromotionCandidate,
    PromotionEvidenceEntry,
    PromotionEvidenceStore,
)


def _clamp_score(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _signal_counts(entry: PromotionEvidenceEntry) -> dict[str, int]:
    return {
        "positive": entry.positive_signal_count,
        "correction": entry.correction_signal_count,
        "failure": entry.failure_signal_count,
        "manual": entry.manual_signal_count,
    }


def _is_pure_negative(entry: PromotionEvidenceEntry) -> bool:
    negative = entry.correction_signal_count + entry.failure_signal_count
    positive = entry.positive_signal_count + entry.manual_signal_count
    return negative > 0 and positive == 0


# D5: constraint_stability lookup (aligned with DESIGN.md §11.2)
_CONSTRAINT_STABILITY: dict[str, float] = {
    "fact": 1.0,
    "decision": 1.0,
    "constraint": 1.0,
    "preference": 0.8,
    "procedure": 0.8,
    "goal": 0.8,
    "event": 0.5,
    "pattern": 0.5,
    "assumption": 0.3,
    "anti_pattern": 0.3,
}
_DEFAULT_CONSTRAINT_STABILITY = 0.65  # no annotation or unknown type


def _cross_task_relevance(usage: dict | None) -> float:
    """D5: cross-task relevance from D11 usage stats.

    Combines recall frequency (log-normalized) with intent diversity.
    Returns 0.0 when no usage data available.
    """
    if not usage:
        return 0.0
    total = usage.get("total_recalls", 0)
    diversity = usage.get("intent_diversity", 0)
    if total <= 0:
        return 0.0
    freq_component = _clamp_score(math.log1p(total) / math.log1p(10))
    diversity_component = min(1.0, diversity / 3)
    return _clamp_score(freq_component * diversity_component)


def _score(
    entry: PromotionEvidenceEntry,
    *,
    usage_stats: dict | None = None,
    constraint_type: str | None = None,
) -> float:
    """D5-enhanced scoring (DESIGN.md §11.2).

    New formula (backward-compatible: usage_stats=None + constraint_type=None
    degrades gracefully — stability defaults to 0.65, cross_task to 0.0):

        0.25 * frequency
        + 0.25 * signal_balance
        + 0.15 * source_confidence
        + 0.10 * consolidation
        + 0.15 * constraint_stability   (NEW)
        + 0.10 * cross_task_relevance   (NEW)
    """
    frequency = _clamp_score(math.log1p(max(0, entry.seen_count)) / math.log1p(6))
    positive_or_manual = entry.positive_signal_count + entry.manual_signal_count
    negative = entry.correction_signal_count + entry.failure_signal_count
    signal_balance = 0.55
    if positive_or_manual > 0:
        signal_balance += 0.3
    if entry.manual_signal_count > 0:
        signal_balance += 0.1
    if negative > 0 and positive_or_manual == 0:
        signal_balance -= 0.25
        if negative > 1:
            signal_balance += 0.25
    source_confidence = 0.75 if entry.source_kind == "memory_file" else 0.5
    consolidation = _clamp_score(len(entry.source_days) / 3)

    # D5: new terms
    stability = _CONSTRAINT_STABILITY.get(
        constraint_type or "", _DEFAULT_CONSTRAINT_STABILITY
    )
    cross_task = _cross_task_relevance(usage_stats)

    return _clamp_score(
        0.25 * frequency
        + 0.25 * _clamp_score(signal_balance)
        + 0.15 * source_confidence
        + 0.10 * consolidation
        + 0.15 * stability
        + 0.10 * cross_task
    )


def rank_promotion_candidates(
    store: PromotionEvidenceStore,
    *,
    min_score: float,
    negative_recurrence_threshold: int,
    min_seen_count: int = 1,
    limit: int | None = None,
    usage_stats: dict[str, dict] | None = None,
    constraint_types: dict[str, str] | None = None,
) -> list[PromotionCandidate]:
    ranked: list[PromotionCandidate] = []
    for entry in store.entries.values():
        if entry.status != "candidate" or not entry.snippet.strip():
            continue
        if entry.seen_count < min_seen_count:
            continue
        reasons: list[str] = []
        if entry.positive_signal_count + entry.manual_signal_count > 0:
            reasons.append("positive_or_manual_signal")
        if _is_pure_negative(entry):
            if entry.seen_count < negative_recurrence_threshold:
                continue
            reasons.append("negative_recurrence")
        if entry.seen_count > 1:
            reasons.append(f"seen_count={entry.seen_count}")
        score = _score(
            entry,
            usage_stats=(usage_stats or {}).get(entry.source_path),
            constraint_type=(constraint_types or {}).get(entry.source_path),
        )
        if score < min_score:
            continue
        # D5: add enhancement reasons
        if constraint_types and entry.source_path in constraint_types:
            ct = constraint_types[entry.source_path]
            if _CONSTRAINT_STABILITY.get(ct, 0) >= 0.8:
                reasons.append("stable_constraint_type")
        if usage_stats and entry.source_path in usage_stats:
            recalls = usage_stats[entry.source_path].get("total_recalls", 0)
            if recalls > 0:
                reasons.append(f"recall_count={recalls}")

        ranked.append(
            PromotionCandidate(
                candidate_id=entry.candidate_id,
                source_path=entry.source_path,
                snippet=entry.snippet,
                snippet_sha256=entry.snippet_sha256,
                claim_sha256=entry.claim_sha256,
                score=score,
                reasons=reasons,
                signal_counts=_signal_counts(entry),
            )
        )
    ranked.sort(
        key=lambda item: (-item.score, -sum(item.signal_counts.values()), item.candidate_id)
    )
    if limit is None:
        return ranked
    return ranked[: max(0, int(limit))]
