"""Shared history cursor types and failure semantics."""

from __future__ import annotations

type HistoryCursor = tuple[int, int]

HISTORY_CURSOR_MAX_INTEGER = (1 << 63) - 1


class HistoryCursorInvalidError(ValueError):
    """Raised when a supplied history cursor is not valid wire input."""


class HistoryCursorInvalidatedError(RuntimeError):
    """Raised when a parsed cursor no longer anchors the requested session."""


__all__ = [
    "HISTORY_CURSOR_MAX_INTEGER",
    "HistoryCursor",
    "HistoryCursorInvalidError",
    "HistoryCursorInvalidatedError",
]
