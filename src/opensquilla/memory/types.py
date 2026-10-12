"""Core data types for the memory system."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

DEFAULT_MEMORY_SEARCH_RESULTS = 6
DEFAULT_MEMORY_SEARCH_MIN_SCORE = 0.35
RELAXED_KEYWORD_MATCH_METADATA_KEY = "relaxed_keyword_match"
RELAXED_KEYWORD_MATCH_METADATA_VALUE = "true"
LEXICAL_GUARANTEE_METADATA_KEY = "lexical_guarantee"
LEXICAL_GUARANTEE_METADATA_VALUE = "true"


def normalize_memory_search_min_score(
    value: object,
    *,
    default: float = DEFAULT_MEMORY_SEARCH_MIN_SCORE,
    strict: bool = False,
) -> float:
    parsed = default
    if value is None:
        return default
    if isinstance(value, (int, float, str)):
        try:
            parsed = float(value)
        except (OverflowError, ValueError) as exc:
            if strict:
                raise ValueError("min_score must be a finite number") from exc
            parsed = default
    elif strict:
        raise TypeError("min_score must be a finite number")
    if not math.isfinite(parsed):
        if strict:
            raise ValueError("min_score must be a finite number")
        parsed = default
    return max(0.0, min(1.0, parsed))


class MemorySource(StrEnum):
    memory = "memory"
    sessions = "sessions"


def normalize_memory_source_filter(value: object, *, allow_all: bool = True) -> MemorySource | None:
    if value is None:
        return None
    if isinstance(value, MemorySource):
        return value
    raw = str(value).strip().lower()
    if not raw:
        return None
    if allow_all and raw == "all":
        return None
    try:
        return MemorySource(raw)
    except ValueError as exc:
        allowed = "'all', 'memory', or 'sessions'" if allow_all else "'memory' or 'sessions'"
        raise ValueError(f"source must be {allowed}") from exc


class SearchMode(StrEnum):
    hybrid = "hybrid"
    fts_only = "fts-only"


class SearchIntent(StrEnum):
    """Intent label for a memory search, used for attribution and filtering."""

    TOOL = "tool"  # memory_search tool path
    ADMIN = "admin"  # CLI / admin queries


@dataclass
class MemorySearchResult:
    """A result from memory search."""

    chunk_id: str
    path: str
    source: MemorySource
    start_line: int
    end_line: int
    snippet: str
    score: float
    vector_score: float | None = None
    text_score: float | None = None
    text: str | None = None
    chunk_hash: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    citation: str | None = None


@dataclass
class MemorySearchOpts:
    max_results: int = DEFAULT_MEMORY_SEARCH_RESULTS
    min_score: float = DEFAULT_MEMORY_SEARCH_MIN_SCORE
    source: MemorySource | None = None


def is_relaxed_keyword_match(result: MemorySearchResult) -> bool:
    return (
        result.metadata.get(RELAXED_KEYWORD_MATCH_METADATA_KEY)
        == RELAXED_KEYWORD_MATCH_METADATA_VALUE
    )


def is_lexical_guaranteed_match(result: MemorySearchResult) -> bool:
    return (
        result.metadata.get(LEXICAL_GUARANTEE_METADATA_KEY)
        == LEXICAL_GUARANTEE_METADATA_VALUE
    )
