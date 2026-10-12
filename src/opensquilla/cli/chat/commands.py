"""Shared chat command policies."""

from __future__ import annotations

BARE_EXIT_WORDS: frozenset[str] = frozenset({":q", "quit", "exit"})


def is_exit_command(value: str) -> bool:
    return value.strip().lower() in BARE_EXIT_WORDS
