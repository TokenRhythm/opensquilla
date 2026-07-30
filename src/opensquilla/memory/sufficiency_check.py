"""Retrieval sufficiency meta-cognition (L3).

Injects a lightweight advisory note into memory_search output when the
result set is likely insufficient for the query's classified intent.
The note does NOT block the agent — it merely informs.

Design decisions (aligned 2026-07-29):
- Injection point: memory_tools.py return layer (after formatting)
- Language: follow query language (CJK ratio > 0.3 → Chinese)
- Format: bounded XML marker <memory_sufficiency_note>
- Trigger: result_count < 3 AND intent_confidence >= 0.7
- Two severities:
  - empty   (count == 0) → stronger prompt to seek external context
  - partial (0 < count < 3) → suggest re-query / broaden

Feature flag: memory.experimental.sufficiency_check (default off).
Requires L2 (constraint_routing) for intent classification data.
"""

from __future__ import annotations

import re

from .constraint_routing import QueryIntent

# ── Thresholds ────────────────────────────────────────────────────────────

SUFFICIENCY_RESULT_THRESHOLD: int = 3
SUFFICIENCY_CONFIDENCE_THRESHOLD: float = 0.7

# ── Language detection ────────────────────────────────────────────────────

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")


def query_is_cjk(query: str) -> bool:
    """Return True if > 30% of non-space characters are CJK."""
    stripped = query.replace(" ", "").replace("\t", "")
    if not stripped:
        return False
    cjk_count = len(_CJK_RE.findall(stripped))
    return (cjk_count / len(stripped)) > 0.3


# ── Trigger check ─────────────────────────────────────────────────────────


def should_emit_sufficiency_note(
    result_count: int,
    confidence: float,
    *,
    enabled: bool = True,
    max_score: float | None = None,
    distinct_evidence_count: int | None = None,
) -> bool:
    """Return True if a sufficiency note should be injected.

    Conditions (all must hold):
    - enabled is True
    - confidence >= SUFFICIENCY_CONFIDENCE_THRESHOLD
    - result_count < SUFFICIENCY_RESULT_THRESHOLD
    """
    if not enabled:
        return False
    if confidence < SUFFICIENCY_CONFIDENCE_THRESHOLD:
        return False
    if result_count == 0:
        return True
    if max_score is not None and max_score >= 0.85:
        return False
    effective_count = (
        distinct_evidence_count
        if distinct_evidence_count is not None
        else result_count
    )
    if effective_count < SUFFICIENCY_RESULT_THRESHOLD:
        return True
    return max_score is not None and max_score < 0.45


# ── Note formatting ───────────────────────────────────────────────────────

_EMPTY_ZH = (
    '<memory_sufficiency_note intent="{intent}" results="0" '
    'confidence="{confidence:.2f}">\n'
    "未找到与当前查询匹配的记忆。\n"
    "建议：这可能是新话题，使用 web_search 获取外部信息，或向用户确认背景。\n"
    "</memory_sufficiency_note>"
)

_EMPTY_EN = (
    '<memory_sufficiency_note intent="{intent}" results="0" '
    'confidence="{confidence:.2f}">\n'
    "No memory results found for the current query.\n"
    "Suggestions: this may be a new topic — consider web_search for external "
    "information, or confirm context with the user.\n"
    "</memory_sufficiency_note>"
)

_PARTIAL_ZH = (
    '<memory_sufficiency_note intent="{intent}" results="{count}" '
    'confidence="{confidence:.2f}">\n'
    "当前检索结果（{count} 条）可能不足以完全覆盖推理需求。\n"
    "建议：调整关键词重新检索，使用 session_search 获取对话上下文，"
    "或告知用户需要更多信息。\n"
    "</memory_sufficiency_note>"
)

_PARTIAL_EN = (
    '<memory_sufficiency_note intent="{intent}" results="{count}" '
    'confidence="{confidence:.2f}">\n'
    "Current retrieval results ({count} items) may be insufficient for the "
    "current reasoning need.\n"
    "Suggestions: retry with adjusted keywords, use session_search for "
    "conversation context, or ask the user for more background.\n"
    "</memory_sufficiency_note>"
)


def format_sufficiency_note(
    query: str,
    result_count: int,
    intent: QueryIntent,
    confidence: float,
) -> str:
    """Format a sufficiency note matching the query language."""
    zh = query_is_cjk(query)
    if result_count == 0:
        template = _EMPTY_ZH if zh else _EMPTY_EN
    else:
        template = _PARTIAL_ZH if zh else _PARTIAL_EN
    return template.format(
        intent=intent.value,
        count=result_count,
        confidence=confidence,
    )


# ── Convenience wrapper ───────────────────────────────────────────────────


def maybe_append_sufficiency_note(
    query: str,
    result_count: int,
    text: str,
    intent: QueryIntent,
    confidence: float,
    *,
    enabled: bool = True,
    max_score: float | None = None,
    distinct_evidence_count: int | None = None,
) -> str:
    """Append a sufficiency note to ``text`` if trigger conditions are met.

    Returns ``text`` unchanged if no note should be emitted.
    """
    if not should_emit_sufficiency_note(
        result_count,
        confidence,
        enabled=enabled,
        max_score=max_score,
        distinct_evidence_count=distinct_evidence_count,
    ):
        return text
    note = format_sufficiency_note(query, result_count, intent, confidence)
    return f"{text}\n\n{note}"
