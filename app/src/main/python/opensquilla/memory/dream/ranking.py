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


def _score(entry: PromotionEvidenceEntry) -> float:
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
    return _clamp_score(
        0.35 * frequency
        + 0.30 * _clamp_score(signal_balance)
        + 0.20 * source_confidence
        + 0.15 * consolidation
    )


def rank_promotion_candidates(
    store: PromotionEvidenceStore,
    *,
    min_score: float,
    negative_recurrence_threshold: int,
    min_seen_count: int = 1,
    limit: int | None = None,
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
        score = _score(entry)
        if score < min_score:
            continue
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
