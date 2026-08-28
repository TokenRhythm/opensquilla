from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Rule:
    id: str
    family: str
    match: dict[str, Any]
    transforms: dict[str, Any]
    filters: dict[str, Any]
    summarize: dict[str, Any]
    failure: dict[str, Any]
    counters: tuple[dict[str, Any], ...]
    output_matches: tuple[dict[str, Any], ...]
    on_empty: str | None
    counter_source: str
    priority: int


@dataclass(frozen=True)
class Reduction:
    inline_text: str
    raw_chars: int
    reduced_chars: int
    ratio: float
    reducer: str | None = None
