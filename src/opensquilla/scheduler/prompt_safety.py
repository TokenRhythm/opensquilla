"""Prompt safety checks shared by scheduler entry points."""

from __future__ import annotations

import re
import unicodedata

import structlog

log = structlog.get_logger(__name__)

_HARD_BLOCK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]"),
    re.compile(r"(curl|wget)\s+.*(\{\{|\\$[\w{])", re.IGNORECASE),
    re.compile(r"rm\s+-rf\s+/", re.IGNORECASE),
    re.compile(r"mkfs\.", re.IGNORECASE),
    re.compile(r":(){ :\|:& };:", re.IGNORECASE),
]

_SOFT_BLOCK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?prior", re.IGNORECASE),
    re.compile(r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE)\b", re.IGNORECASE),
]


def scan_cron_prompt(task: str) -> tuple[bool, str]:
    """Return whether a scheduled prompt should be rejected."""
    for char in task:
        cat = unicodedata.category(char)
        if cat in ("Cf", "Mn", "Cc") and char not in ("\n", "\r", "\t"):
            log.warning("cron_prompt_blocked", pattern="invisible_unicode", char=repr(char))
            return True, f"Blocked: invisible unicode character detected ({repr(char)})"

    for pattern in _HARD_BLOCK_PATTERNS:
        if pattern.search(task):
            log.warning("cron_prompt_blocked", pattern=pattern.pattern)
            return True, f"Blocked: dangerous pattern detected ({pattern.pattern})"

    for pattern in _SOFT_BLOCK_PATTERNS:
        if pattern.search(task):
            log.warning("cron_prompt_blocked", pattern=pattern.pattern, severity="soft")
            return True, f"Blocked: potential injection pattern ({pattern.pattern})"

    return False, ""
