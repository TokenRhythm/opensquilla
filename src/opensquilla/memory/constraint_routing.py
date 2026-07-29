"""Constraint-aware retrieval routing (L2).

Implements query intent classification and constraint-type boost for
memory search results. Depends on L1 constraint_type annotations.

Design principles (aligned 2026-07-29):
- Boost range [0.85, 1.8] — never fully suppress (D3)
- Confidence < 0.6 → no boost (neutral, not debuff)
- Confidence is None (Signal Gate skipped) → no boost
- Feature flag off → entire module is a no-op
- L1 off → all chunks "fact" → boost 1.0 → no-op
"""

from __future__ import annotations

import re
from enum import StrEnum

from .types import ConstraintType, MemorySearchResult

# ── Query Intent Classification ───────────────────────────────────────────


class QueryIntent(StrEnum):
    """Intent classification for memory search queries."""

    continue_task = "continue_task"
    retrieve_rationale = "retrieve_rationale"
    avoid_failure = "avoid_failure"
    transfer_knowledge = "transfer_knowledge"
    general = "general"


# Keyword patterns for each intent, ordered by priority (first match wins).
# Each entry: (compiled_regex, intent, confidence)
_INTENT_PATTERNS: list[tuple[re.Pattern[str], QueryIntent, float]] = [
    # avoid_failure — highest priority (error avoidance is safety-critical)
    (
        re.compile(
            r"\b(problem|error|wrong|bug|fail|crash|broken|issue)\b"
            r"|(问题|错误|失败|崩溃|故障|异常|报错)",
            re.IGNORECASE,
        ),
        QueryIntent.avoid_failure,
        0.7,
    ),
    # continue_task — resume/continue
    (
        re.compile(
            r"\b(continue|resume|next step|pick up|where (was|are) we)\b"
            r"|(继续|接着|上次|接下来|下一步|接着做)",
            re.IGNORECASE,
        ),
        QueryIntent.continue_task,
        0.7,
    ),
    # retrieve_rationale — why/reason
    (
        re.compile(
            r"\b(why|reason|rationale|because|explain)\b"
            r"|(为什么|原因|怎么回事|为何|理由)",
            re.IGNORECASE,
        ),
        QueryIntent.retrieve_rationale,
        0.7,
    ),
    # transfer_knowledge — similar/before
    (
        re.compile(
            r"\b(similar|like before|same as|analogous|experience)\b"
            r"|(类似|有没有经验|以前|之前做过|同样)",
            re.IGNORECASE,
        ),
        QueryIntent.transfer_knowledge,
        0.6,
    ),
]


def classify_query_intent(query: str) -> tuple[QueryIntent, float]:
    """Classify query intent using keyword heuristics.

    Returns (intent, confidence). Default: (general, 0.5).
    Confidence < 0.7 means L3 sufficiency check won't trigger (D4).
    """
    for pattern, intent, confidence in _INTENT_PATTERNS:
        if pattern.search(query):
            return intent, confidence
    return QueryIntent.general, 0.5


# ── Boost Map ─────────────────────────────────────────────────────────────

# Boost values per (intent, constraint_type).
# Range: [0.85, 1.8] (D3). Missing entries default to 1.0 (neutral).
QUERY_INTENT_BOOST: dict[QueryIntent, dict[ConstraintType, float]] = {
    QueryIntent.continue_task: {
        ConstraintType.goal: 1.5,
        ConstraintType.event: 1.3,  # "上次做到哪了" needs event context
        ConstraintType.decision: 1.2,
    },
    QueryIntent.retrieve_rationale: {
        ConstraintType.decision: 1.5,
    },
    QueryIntent.avoid_failure: {
        # v0.8: ConstraintType.anti_pattern: 1.8,
        # v0.8: ConstraintType.constraint: 1.3,
    },
    QueryIntent.transfer_knowledge: {
        ConstraintType.decision: 1.2,
        # v0.8: ConstraintType.pattern: 1.8,
    },
    QueryIntent.general: {},
}

# ── Boost Application ─────────────────────────────────────────────────────

BOOST_MIN = 0.85
BOOST_MAX = 1.8
CONFIDENCE_THRESHOLD = 0.6


def _get_constraint_type(result: MemorySearchResult) -> ConstraintType:
    """Extract constraint_type from result metadata, defaulting to fact."""
    raw = result.metadata.get("constraint_type", "fact")
    try:
        return ConstraintType(raw)
    except ValueError:
        return ConstraintType.fact


def _get_constraint_confidence(result: MemorySearchResult) -> float | None:
    """Extract constraint_confidence from result metadata.

    Returns None if not present or not parseable (means "not annotated").
    """
    raw = result.metadata.get("constraint_confidence")
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def compute_boost(
    result: MemorySearchResult,
    query_intent: QueryIntent,
    *,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> float:
    """Compute the boost factor for a single result.

    Rules:
    - confidence < threshold → 1.0 (neutral)
    - confidence is None → 1.0 (neutral, Signal Gate skipped)
    - Otherwise → boost from QUERY_INTENT_BOOST, clipped to [0.85, 1.8]
    """
    confidence = _get_constraint_confidence(result)
    if confidence is None or confidence < confidence_threshold:
        return 1.0

    ctype = _get_constraint_type(result)
    boost_map = QUERY_INTENT_BOOST.get(query_intent, {})
    boost = boost_map.get(ctype, 1.0)
    return max(BOOST_MIN, min(BOOST_MAX, boost))


def apply_constraint_boost(
    results: list[MemorySearchResult],
    query_intent: QueryIntent,
    *,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> list[MemorySearchResult]:
    """Apply constraint-type boost to search results and re-sort.

    Modifies result scores in-place and returns the same list sorted
    by descending score. If query_intent is 'general', returns unchanged.
    """
    if query_intent == QueryIntent.general:
        return results

    for result in results:
        boost = compute_boost(result, query_intent, confidence_threshold=confidence_threshold)
        if boost != 1.0:
            result.score *= boost

    results.sort(key=lambda r: r.score, reverse=True)
    return results


# ── Provenance Marker (D9) ────────────────────────────────────────────────


def should_add_provenance_marker(result: MemorySearchResult) -> bool:
    """Determine if a result should get a provenance marker.

    Returns True when:
    - constraint_type is not the default "fact"
    - constraint_confidence is present (not None)
    """
    ctype = _get_constraint_type(result)
    confidence = _get_constraint_confidence(result)
    if confidence is None:
        return False
    if ctype == ConstraintType.fact:
        return False
    return True


def format_provenance_marker(result: MemorySearchResult, content: str) -> str:
    """Wrap content with a lightweight XML provenance marker.

    Format:
        <memory_result type="decision" confidence="0.80">
        [content]
        </memory_result>
    """
    ctype = _get_constraint_type(result)
    confidence = _get_constraint_confidence(result)
    conf_str = f"{confidence:.2f}" if confidence is not None else "unknown"
    return (
        f'<memory_result type="{ctype.value}" confidence="{conf_str}">\n'
        f"{content}\n"
        f"</memory_result>"
    )
