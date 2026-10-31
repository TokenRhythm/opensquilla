"""Canonical three-segment text extraction shared by offline and online code."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar


HISTORY_MAX_TURNS = 4
HISTORY_SEPARATOR = "\n[TURN]\n"
SegmentKind = Literal["current_request", "recent_history", "previous_answer"]
SEGMENT_KINDS: tuple[SegmentKind, SegmentKind, SegmentKind] = (
    "current_request",
    "recent_history",
    "previous_answer",
)
TokenT = TypeVar("TokenT")


@dataclass(frozen=True, slots=True)
class TextSegments:
    current_request: str
    recent_history: str
    previous_answer: str

    def as_tuple(self) -> tuple[str, str, str]:
        return self.current_request, self.recent_history, self.previous_answer


def canonicalize_text(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"text value must be str or None, got {type(value).__name__}")
    text = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    return text.strip()


def truncate_content_tokens(
    kind: SegmentKind,
    tokens: Sequence[TokenT],
    *,
    max_content_tokens: int = 510,
) -> list[TokenT]:
    """Apply the versioned segment policy before model special tokens are added.

    Current requests retain approximately 3:1 head/tail content, previous
    answers retain 1:1, and history retains the most recent content.
    """
    if max_content_tokens <= 0:
        raise ValueError("max_content_tokens must be positive")
    values = list(tokens)
    if len(values) <= max_content_tokens:
        return values
    if kind == "recent_history":
        return values[-max_content_tokens:]
    if kind == "current_request":
        head = (max_content_tokens * 3 + 3) // 4
    elif kind == "previous_answer":
        head = (max_content_tokens + 1) // 2
    else:
        raise ValueError(f"unsupported segment kind: {kind!r}")
    tail = max_content_tokens - head
    return values[:head] + (values[-tail:] if tail else [])


def sample_input(row: Mapping[str, object]) -> Mapping[str, object]:
    value = row.get("input")
    if value is None:
        return row
    if not isinstance(value, Mapping):
        raise TypeError("row.input must be a mapping")
    return value


def extract_text_segments(row: Mapping[str, object]) -> TextSegments:
    payload = sample_input(row)
    current = canonicalize_text(payload.get("current_request"))
    if not current:
        raise ValueError("input.current_request must be non-empty")

    raw_history = payload.get("history_user") or []
    if isinstance(raw_history, (str, bytes)) or not isinstance(raw_history, Sequence):
        raise TypeError("input.history_user must be a sequence of strings")
    turns = [canonicalize_text(value) for value in raw_history]
    turns = [value for value in turns if value][-HISTORY_MAX_TURNS:]
    history = HISTORY_SEPARATOR.join(turns)
    previous = canonicalize_text(payload.get("previous_answer"))
    return TextSegments(current, history, previous)
