"""Constraint type classifier for memory chunks (L1).

Implements the three-tier classification pipeline:
1. Signal Gate: skip low-signal chunks (saves 30-40% LLM calls)
2. LLM classification (primary path)
3. Heuristic keyword matching (fallback)

Design principles (aligned 2026-07-29):
- Wrong classification is worse than no classification
- Confidence < 0.6 → L2 will not apply boost (neutral)
- Feature flag off → entire module is a no-op
- Frontmatter user override has highest priority (confidence = 1.0)
"""

from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable

from .types import CORE_CONSTRAINT_TYPES, ConstraintType

logger = logging.getLogger(__name__)

# Type alias for the LLM call function injected by the caller.
# Signature: async (prompt: str) -> str
LlmCallFn = Callable[[str], Awaitable[str]]

# ── Signal Gate (D8) ──────────────────────────────────────────────────────

_MIN_CLASSIFY_LENGTH = 20
_MIN_ALPHA_CJK_RATIO = 0.3

# Patterns that indicate heartbeat / status / low-signal messages
_STATUS_PATTERNS = re.compile(
    r"^(HEARTBEAT_OK|NO_REPLY|ok|done|yes|no|sure|got it|understood|"
    r"acknowledged|success|error|failed|ready)$",
    re.IGNORECASE,
)

# Patterns that indicate structured tool output (not natural language)
_TOOL_OUTPUT_RE = re.compile(
    r"(exit_code=\d+|traceback|\bstdout\b|\bstderr\b)"
    r"|^[\d\s.,;:=+\-*/(){}\[\]<>\"'!@#$%^&|\\~`]+$"
)


def _alpha_cjk_ratio(text: str) -> float:
    """Fraction of characters that are alphabetic or CJK."""
    if not text:
        return 0.0
    alpha_cjk = sum(
        1
        for ch in text
        if ch.isalpha()
        or "\u4e00" <= ch <= "\u9fff"  # CJK Unified Ideographs
        or "\u3040" <= ch <= "\u30ff"  # Hiragana + Katakana
        or "\uac00" <= ch <= "\ud7af"  # Hangul
    )
    # Exclude whitespace from denominator for better ratio
    non_ws = len(text) - text.count(" ") - text.count("\n") - text.count("\t")
    if non_ws == 0:
        return 0.0
    return alpha_cjk / non_ws


def should_classify(text: str) -> bool:
    """Signal Gate: return False for low-signal chunks that should skip classification."""
    stripped = text.strip()
    if len(stripped) < _MIN_CLASSIFY_LENGTH:
        return False
    if _STATUS_PATTERNS.match(stripped):
        return False
    if _TOOL_OUTPUT_RE.search(stripped):
        return False
    if _alpha_cjk_ratio(stripped) < _MIN_ALPHA_CJK_RATIO:
        return False
    return True


# ── Heuristic Classification (fallback) ───────────────────────────────────

# (keywords, constraint_type, confidence) — checked in order, first match wins.
# Ordered by specificity: earlier rules are more distinctive.
# Temporal markers (event) are checked before procedure because "yesterday we
# deployed X" is an event report, not a how-to guide.
_HEURISTIC_RULES: list[tuple[list[str], ConstraintType, float]] = [
    # decision — explicit choice language (highest specificity)
    (
        ["decided", "chose", "we went with", "we chose", "opted for",
         "选择", "决定", "选了", "采用了", "最终选"],
        ConstraintType.decision,
        0.6,
    ),
    # preference — user style/like
    (
        ["prefer", "like to", "always use", "preference",
         "偏好", "喜欢", "习惯", "倾向于"],
        ConstraintType.preference,
        0.6,
    ),
    # event — temporal references (before procedure: "yesterday deployed" = event)
    (
        ["yesterday", "last week", "last month", "last night", "ago", "just now",
         "昨天", "上周", "上个月", "刚刚", "刚才", "前天"],
        ConstraintType.event,
        0.5,
    ),
    # procedure — how-to / steps
    (
        ["step ", "how to", "how do i", "install", "deploy",
         "步骤", "流程", "安装", "部署", "配置", "执行"],
        ConstraintType.procedure,
        0.6,
    ),
    # goal — targets / tasks / intentions
    (
        ["goal", "todo", "task", "target", "objective", "plan to",
         "目标", "任务", "计划", "继续", "接下来", "待办", "要做"],
        ConstraintType.goal,
        0.5,
    ),
]


def heuristic_classify(text: str) -> tuple[ConstraintType, float]:
    """Keyword-based heuristic classification. Returns (type, confidence)."""
    lower = text.lower()
    for keywords, ctype, confidence in _HEURISTIC_RULES:
        if any(kw in lower for kw in keywords):
            return ctype, confidence
    return ConstraintType.fact, 0.4


# ── LLM Classification (primary) ──────────────────────────────────────────

_CLASSIFY_PROMPT = """\
Classify this memory chunk into exactly one type:
[fact, event, preference, decision, procedure, goal]

fact: a stable objective fact
event: something that happened at a specific time
preference: a user's ongoing preference or style choice
decision: a choice made with reasoning
procedure: a step-by-step process or how-to
goal: a target, intention, or task to be done

Chunk:
{chunk_text}

Reply with ONLY the type name."""

_LLM_CONFIDENCE = 0.8
_LLM_MAX_CHUNK_CHARS = 2000  # truncate to avoid token limits


async def llm_classify(
    text: str,
    llm_call: LlmCallFn,
) -> tuple[ConstraintType, float] | None:
    """Attempt LLM classification. Returns None on failure (caller falls back)."""
    prompt = _CLASSIFY_PROMPT.format(chunk_text=text[:_LLM_MAX_CHUNK_CHARS])
    try:
        response = await llm_call(prompt)
        normalized = response.strip().lower().strip("\"'` ")
        # Remove any trailing punctuation or explanation
        normalized = re.sub(r"[^a-z_].*$", "", normalized)
        if not normalized:
            return None
        try:
            ctype = ConstraintType(normalized)
        except ValueError:
            return None
        # Only core types are valid for v0.7
        if ctype not in CORE_CONSTRAINT_TYPES:
            return None
        return ctype, _LLM_CONFIDENCE
    except Exception:
        logger.debug("llm_constraint_classify_failed", exc_info=True)
        return None


# ── Frontmatter Parsing (D6: user override) ───────────────────────────────

_FRONTMATTER_BLOCK_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n",
    re.DOTALL,
)
_CONSTRAINT_TYPE_RE = re.compile(
    r"^constraint_type:\s*(\S+)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def parse_frontmatter_constraint(text: str) -> ConstraintType | None:
    """Extract constraint_type from Markdown frontmatter if present.

    Returns None if no valid frontmatter constraint_type found.
    """
    m = _FRONTMATTER_BLOCK_RE.match(text)
    if not m:
        return None
    fm_block = m.group(1)
    ct_match = _CONSTRAINT_TYPE_RE.search(fm_block)
    if not ct_match:
        return None
    raw = ct_match.group(1).strip().lower()
    try:
        return ConstraintType(raw)
    except ValueError:
        return None


# ── Unified Classification Entry Point ────────────────────────────────────


async def classify_constraint(
    text: str,
    *,
    llm_call: LlmCallFn | None = None,
) -> tuple[ConstraintType, float | None]:
    """Classify a memory chunk into a constraint type.

    Pipeline:
    1. Frontmatter override → (type, 1.0)
    2. Signal Gate → skip → (fact, None)
    3. LLM classification → (type, 0.8)
    4. Heuristic fallback → (type, 0.4-0.6)

    Returns (constraint_type, confidence).
    Confidence is None when skipped by Signal Gate (means "not annotated").
    """
    # 1. User override via frontmatter (highest priority)
    fm_type = parse_frontmatter_constraint(text)
    if fm_type is not None:
        return fm_type, 1.0

    # 2. Signal Gate
    if not should_classify(text):
        return ConstraintType.fact, None

    # 3. LLM classification (if available)
    if llm_call is not None:
        result = await llm_classify(text, llm_call)
        if result is not None:
            return result

    # 4. Heuristic fallback
    return heuristic_classify(text)


def classify_constraint_sync(text: str) -> tuple[ConstraintType, float | None]:
    """Synchronous heuristic-only classification (no LLM).

    Used for testing and as a fast path when LLM is not configured.
    """
    # 1. Frontmatter override
    fm_type = parse_frontmatter_constraint(text)
    if fm_type is not None:
        return fm_type, 1.0

    # 2. Signal Gate
    if not should_classify(text):
        return ConstraintType.fact, None

    # 3. Heuristic only
    return heuristic_classify(text)
