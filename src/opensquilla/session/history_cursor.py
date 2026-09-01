"""Shared history cursor types and failure semantics."""

from __future__ import annotations

type HistoryCursor = tuple[int, int]


class HistoryCursorInvalidError(ValueError):
    """Raised when a supplied history cursor is not valid wire input."""


class HistoryCursorInvalidatedError(RuntimeError):
    """Raised when a parsed cursor no longer anchors the requested session."""


__all__ = [
    "HistoryCursor",
    "HistoryCursorInvalidError",
    "HistoryCursorInvalidatedError",
]
