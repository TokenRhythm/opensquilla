"""Canonical predicates for searchable memory source paths."""

from __future__ import annotations

from pathlib import Path

from .types import MemorySource


def is_memory_source_path(path: str) -> bool:
    """Return True for OpenSquilla curated memory source files."""
    rel = Path(path)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        return False
    if rel.parts == ("MEMORY.md",):
        return True
    return (
        len(rel.parts) >= 2
        and rel.parts[0] == "memory"
        and rel.suffix == ".md"
        and not any(part.startswith(".") for part in rel.parts[1:])
    )


def is_session_source_path(path: str) -> bool:
    """Return True for derived session transcript source documents."""
    rel = Path(path)
    return (
        not rel.is_absolute()
        and len(rel.parts) == 3
        and rel.parts[0] == "sessions"
        and rel.suffix == ".md"
        and not any(part in {"", ".", ".."} or part.startswith(".") for part in rel.parts)
    )


def is_searchable_source_path(source: MemorySource | str, path: str) -> bool:
    """Return True when an indexed result belongs to a searchable source."""
    try:
        memory_source = source if isinstance(source, MemorySource) else MemorySource(source)
    except ValueError:
        return False
    if memory_source is MemorySource.memory:
        return is_memory_source_path(path)
    if memory_source is MemorySource.sessions:
        return is_session_source_path(path)
    return False
