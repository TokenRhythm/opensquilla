"""Canonical predicates for searchable memory source paths."""

from __future__ import annotations

import re
from pathlib import Path

from .types import MemorySource

_LEGACY_RAW_FALLBACK_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(reset|compact)-\d+\.md$")
_LEGACY_TURN_CAPTURE_REQUIRED_MARKERS = (
    "# Turn Capture",
    "- source_kind: turn_capture",
    "- schema: turn-capture-v1",
)
LEGACY_CAPTURE_SAMPLE_CHARS = 1000


def is_legacy_raw_fallback(path: str | Path, content: str) -> bool:
    """Recognize generated raw flushes by both their name and first-line header."""
    return bool(
        _LEGACY_RAW_FALLBACK_NAME_RE.fullmatch(Path(path).name)
        and content.startswith("# Raw flush (")
    )


def is_legacy_turn_capture(content: str) -> bool:
    """Recognize the old generated turn-capture schema, never a directory name alone."""
    sample = content[:LEGACY_CAPTURE_SAMPLE_CHARS]
    return all(marker in sample for marker in _LEGACY_TURN_CAPTURE_REQUIRED_MARKERS)


def is_legacy_memory_capture_candidate(path: str) -> bool:
    """Limit content probing to the two historical generated-file locations."""
    rel = Path(path)
    if not is_memory_source_path(path):
        return False
    return (
        len(rel.parts) == 2
        and bool(_LEGACY_RAW_FALLBACK_NAME_RE.fullmatch(rel.name))
    ) or (len(rel.parts) >= 3 and rel.parts[:2] == ("memory", "archive"))


def is_legacy_memory_capture(path: str, content: str) -> bool:
    """Classify a logical curated path without modifying its Markdown source."""
    if not is_legacy_memory_capture_candidate(path):
        return False
    rel = Path(path)
    if len(rel.parts) == 2:
        return is_legacy_raw_fallback(rel, content)
    return is_legacy_turn_capture(content)


def is_legacy_memory_capture_file(path: Path, *, relative_path: str) -> bool:
    """Read only the bounded identifying prefix; callers own I/O error policy."""
    if not is_legacy_memory_capture_candidate(relative_path):
        return False
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        sample = handle.read(LEGACY_CAPTURE_SAMPLE_CHARS)
    return is_legacy_memory_capture(relative_path, sample)


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
