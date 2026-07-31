"""Cross-batch aggregation: turn per-batch LLM verdicts into a profile payload.

Pure arithmetic over :class:`BatchAnalysis` values (§1.4 of the offline plan).
No IO and no provider import: the orchestrator supplies the producer-owned
baseline (for shape and the permission snapshot) and persists the returned dict
via ``store``. The worked example in the plan is the contract these functions
must reproduce exactly; see ``test_user_profile_builder``.
"""

from __future__ import annotations

import copy
from collections import Counter
from typing import Any

from opensquilla.squilla_router.user_profile.schema import (
    COST_SENSITIVITY_VALUES,
    PRAISE,
    TRADEOFF_VALUES,
    UNKNOWN_CAPABILITY,
    BatchAnalysis,
    ModelMention,
)

# Cap evidence lists so a large run does not bloat the file; provenance only.
_MAX_EVIDENCE = 10


def _round(value: float, places: int = 4) -> float:
    return round(value, places)


def _capability_prior(
    batches: list[BatchAnalysis], top_n: int
) -> tuple[dict[str, float], list[str], float]:
    """Proportion of non-unknown labels per axis, top-N, weights may sum < 1.

    Denominator is the count of *labeled, non-unknown* sessions (§1.4: 63 read,
    3 unknown -> /60). The long tail beyond ``top_n`` is dropped, so the kept
    weights sum to less than 1.
    """

    counts: Counter[str] = Counter()
    evidence: dict[str, list[str]] = {}
    confidences: list[float] = []
    for batch in batches:
        if not batch.ok:
            continue
        for label in batch.session_labels:
            if label.capability == UNKNOWN_CAPABILITY:
                continue
            counts[label.capability] += 1
            confidences.append(label.confidence)
            evidence.setdefault(label.capability, []).append(label.session_id)
    denominator = sum(counts.values())
    if denominator == 0:
        return {}, [], 0.0
    # Rank by proportion then name for deterministic tie-breaking.
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[: max(0, top_n)]
    prior = {cap: _round(count / denominator) for cap, count in ranked}
    kept_evidence: list[str] = []
    for cap, _count in ranked:
        for session_id in evidence.get(cap, []):
            kept_evidence.append(f"session:{session_id}")
            if len(kept_evidence) >= _MAX_EVIDENCE:
                break
        if len(kept_evidence) >= _MAX_EVIDENCE:
            break
    confidence = _round(sum(confidences) / len(confidences)) if confidences else 0.0
    return prior, kept_evidence, confidence


def _tradeoff(
    batches: list[BatchAnalysis],
) -> tuple[str | None, float, int, int, list[str]]:
    """Majority vote over batch tradeoffs; tie or unknown-majority -> ``None``.

    Returns ``(winner, confidence, winner_votes, total_votes)``. Confidence is
    the mean confidence of the batches that voted for the winner.
    """

    votes: Counter[str] = Counter()  # includes an "unknown" bucket
    ok_batches = [b for b in batches if b.ok]
    for batch in ok_batches:
        key = (
            batch.tradeoff
            if batch.tradeoff in TRADEOFF_VALUES and batch.tradeoff_session_ids
            else "unknown"
        )
        votes[key] += 1
    total = len(ok_batches)
    if not votes:
        return None, 0.0, 0, total, []
    top_count = max(votes.values())
    leaders = [value for value, count in votes.items() if count == top_count]
    # Unknown wins, or a tie among real values -> no confident signal.
    if len(leaders) != 1 or leaders[0] == "unknown":
        return None, 0.0, 0, total, []
    winner = leaders[0]
    confidences = [b.tradeoff_confidence for b in ok_batches if b.tradeoff == winner]
    confidence = _round(sum(confidences) / len(confidences)) if confidences else 0.0
    evidence = [
        f"session:{session_id}"
        for batch in ok_batches
        if batch.tradeoff == winner
        for session_id in batch.tradeoff_session_ids
    ]
    return winner, confidence, top_count, total, list(dict.fromkeys(evidence))[:_MAX_EVIDENCE]


def _cost_sensitivity(
    batches: list[BatchAnalysis],
) -> tuple[str | None, float, int, int]:
    """Majority vote over batch cost_sensitivity; tie or unknown-majority -> ``None``.

    Returns ``(winner, confidence, winner_votes, total_votes)``. Confidence is
    the mean confidence of the batches that voted for the winner.
    """

    votes: Counter[str] = Counter()  # includes an "unknown" bucket
    ok_batches = [b for b in batches if b.ok]
    for batch in ok_batches:
        key = (
            batch.cost_sensitivity
            if batch.cost_sensitivity in COST_SENSITIVITY_VALUES
            else "unknown"
        )
        votes[key] += 1
    total = len(ok_batches)
    if not votes:
        return None, 0.0, 0, total
    top_count = max(votes.values())
    leaders = [value for value, count in votes.items() if count == top_count]
    # Unknown wins, or a tie among real values -> no confident signal.
    if len(leaders) != 1 or leaders[0] == "unknown":
        return None, 0.0, 0, total
    winner = leaders[0]
    confidences = [
        b.cost_sensitivity_confidence for b in ok_batches if b.cost_sensitivity == winner
    ]
    confidence = _round(sum(confidences) / len(confidences)) if confidences else 0.0
    return winner, confidence, top_count, total


def _model_lists(
    batches: list[BatchAnalysis],
) -> tuple[list[str], list[str], dict[str, list[str]], dict[str, float]]:
    """Split named models into positive/negative by consistent, repeated mention.

    The prompt owns the repeated/consistent threshold; a parsed model needs only
    valid evidence session ids in one direction and *none* in the other. A model
    evaluated in both directions lands in neither (§1.4 glm-5.2).
    """

    praise_sessions: dict[str, set[str]] = {}
    blame_sessions: dict[str, set[str]] = {}
    confidence_max: dict[str, float] = {}

    def record(mention: ModelMention) -> None:
        target_sets = praise_sessions if mention.direction == PRAISE else blame_sessions
        bucket = target_sets.setdefault(mention.model_id, set())
        bucket.update(mention.session_ids)
        confidence_max[mention.model_id] = max(
            confidence_max.get(mention.model_id, 0.0), mention.confidence
        )

    for batch in batches:
        if not batch.ok:
            continue
        for mention in batch.model_mentions:
            record(mention)

    positive: list[str] = []
    negative: list[str] = []
    evidence: dict[str, list[str]] = {}
    models = set(praise_sessions) | set(blame_sessions)
    for model in sorted(models):
        praise_count = len(praise_sessions.get(model, set()))
        blame_count = len(blame_sessions.get(model, set()))
        if praise_count > 0 and blame_count == 0:
            positive.append(model)
            evidence[model] = [f"session:{s}" for s in sorted(praise_sessions.get(model, set()))]
        elif blame_count > 0 and praise_count == 0:
            negative.append(model)
            evidence[model] = [f"session:{s}" for s in sorted(blame_sessions.get(model, set()))]
    return positive, negative, evidence, confidence_max


def build_profile(
    *,
    batches: list[BatchAnalysis],
    base_profile: dict,
    sessions_read: int,
    day: str,
    version: str,
    top_n: int,
    window_days: int,
) -> dict:
    """Assemble the versioned profile payload (schema per offline plan §1.1).

    ``base_profile`` supplies the shape, every default
    the LLM did not infer, and the permission block (an effective-default
    snapshot for replay — runtime hard-filtering still reads live config, so the
    read seam never overlays this permission). Inferred fields overwrite the
    ``preference``/``history`` sections; provenance rides in ``_meta``, which the
    consumer does not need to read.
    """

    payload = copy.deepcopy(base_profile)
    payload["profile_version"] = version
    payload.pop("profile_source", None)

    prior, prior_evidence, prior_conf = _capability_prior(batches, top_n)
    tradeoff, tradeoff_conf, vote_num, vote_den, tradeoff_evidence = _tradeoff(batches)
    cost_sens, cost_conf, cost_num, cost_den = _cost_sensitivity(batches)
    positive, negative, model_evidence, model_conf = _model_lists(batches)

    preference = dict(payload.get("preference") or {})
    # ``None`` is an honest "no signal": written as null, read as absent (the
    # seam keeps the baseline default rather than overlaying null).
    preference["quality_latency_tradeoff"] = tradeoff
    preference["cost_sensitivity"] = cost_sens
    payload["preference"] = preference

    history = dict(payload.get("history") or {})
    history["capability_prior"] = prior
    history["positive_model_ids"] = positive
    history["negative_model_ids"] = negative
    history["feedback_count"] = sessions_read
    history["last_updated_at"] = day
    payload["history"] = history

    fields: dict[str, Any] = {}
    if tradeoff is not None:
        fields["preference.quality_latency_tradeoff"] = {
            "confidence": tradeoff_conf,
            "vote": f"{vote_num}/{vote_den}",
            "evidence": tradeoff_evidence,
        }
    if cost_sens is not None:
        fields["preference.cost_sensitivity"] = {
            "confidence": cost_conf,
            "vote": f"{cost_num}/{cost_den}",
        }
    if prior:
        fields["history.capability_prior"] = {
            "confidence": prior_conf,
            "evidence": prior_evidence,
        }
    if positive:
        pos_conf = _round(max((model_conf.get(m, 0.0) for m in positive), default=0.0))
        pos_evidence: list[str] = []
        for model in positive:
            pos_evidence.extend(model_evidence.get(model, []))
        fields["history.positive_model_ids"] = {
            "confidence": pos_conf,
            "mentions": len(set(pos_evidence)),
            "evidence": pos_evidence[:_MAX_EVIDENCE],
        }
    if negative:
        neg_conf = _round(max((model_conf.get(m, 0.0) for m in negative), default=0.0))
        neg_evidence: list[str] = []
        for model in negative:
            neg_evidence.extend(model_evidence.get(model, []))
        fields["history.negative_model_ids"] = {
            "confidence": neg_conf,
            "mentions": len(set(neg_evidence)),
            "evidence": neg_evidence[:_MAX_EVIDENCE],
        }

    payload["_meta"] = {
        "window_days": window_days,
        "batches": len(batches),
        "fields": fields,
    }
    return payload


__all__ = ["build_profile"]
