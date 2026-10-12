"""Leaf bootstrap types shared across prompt assembly and observability."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BootstrapFileReport:
    """Content-safe diagnostics for one injected bootstrap markdown file."""

    filename: str
    raw_chars: int = 0
    injected_chars: int = 0
    truncated: bool = False
    truncation_cause: str | None = None
    skipped_reason: str | None = None


__all__ = ["BootstrapFileReport"]
