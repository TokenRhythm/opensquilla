"""Smart-routing refusal gate: URL / backtick / complex-keyword detection."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_DEFAULT_COMPLEX_KEYWORDS: tuple[str, ...] = ("refactor", "migrate", "security audit")
_URL_RE = re.compile(r"https?://", re.IGNORECASE)


@dataclass
class RefusalDecision:
    """Why a prompt was refused and the user-visible message to return."""

    reason: str
    user_message: str


def _keyword_pattern(keywords: Sequence[str]) -> re.Pattern[str]:
    """Word-boundary, case-insensitive alternation over the keyword set."""
    alternation = "|".join(re.escape(k.strip()) for k in keywords if k.strip())
    return re.compile(rf"\b(?:{alternation})\b", re.IGNORECASE)


def should_refuse(
    prompt: str,
    keywords: Sequence[str] | None = None,
) -> RefusalDecision | None:
    """Return a ``RefusalDecision`` when ``prompt`` trips a refusal signal.

    Signals (checked in order): URL scheme, backtick / code-fence marker,
    complex keyword match. Returns ``None`` when the prompt is clean.
    ``keywords`` overrides the default complex-keyword set when provided.
    """
    if not isinstance(prompt, str) or not prompt:
        return None

    if _URL_RE.search(prompt):
        return RefusalDecision(
            reason="url_detected",
            user_message=(
                "I can't fetch URLs directly. Paste the relevant content and I'll work from that."
            ),
        )

    if "`" in prompt:
        return RefusalDecision(
            reason="code_fence_detected",
            user_message=(
                "Code fences (`...`) look like pre-formatted input; please "
                "send the code as plain text or a structured attachment."
            ),
        )

    kws = tuple(keywords) if keywords is not None else _DEFAULT_COMPLEX_KEYWORDS
    kws = tuple(k for k in kws if k and k.strip())
    if kws and _keyword_pattern(kws).search(prompt):
        return RefusalDecision(
            reason="complex_keyword_detected",
            user_message=(
                "This request looks like a complex change ("
                + ", ".join(kws)
                + "). Please break it into smaller, scoped asks."
            ),
        )

    return None
