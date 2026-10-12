"""High-level retrieval combining vector + keyword + time-range filtering."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .source_paths import is_searchable_source_path
from .store import LongTermMemoryStore
from .types import (
    MemorySearchOpts,
    MemorySearchResult,
    MemorySource,
    SearchIntent,
    is_lexical_guaranteed_match,
    is_relaxed_keyword_match,
)

# Matches YYYY-MM-DD.md or YYYY-MM-DD-<slug>.md at the basename.
# The date must prefix the basename; embedded dates elsewhere do not match.
_DATED_FILENAME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:-[a-z0-9][a-z0-9_-]*)?\.md")


def _match_dated_basename(path: str) -> re.Match[str] | None:
    return _DATED_FILENAME_RE.fullmatch(Path(path).name)


def _parse_dated_path(path: str) -> datetime | None:
    """Extract date from memory/YYYY-MM-DD.md or memory/YYYY-MM-DD-<slug>.md."""
    m = _match_dated_basename(path)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=UTC)
        except ValueError:
            return None
    return None


def _is_evergreen(path: str) -> bool:
    """MEMORY.md and non-dated memory/ files are evergreen (no temporal decay)."""
    basename = Path(path).name
    if basename == "MEMORY.md":
        return True
    if "memory/" in path and _match_dated_basename(path) is None:
        return True
    return False


def _temporal_decay(
    score: float, path: str, mtime: float | None, half_life_days: float = 30.0
) -> float:
    """Apply exponential decay to score based on file age."""
    if _is_evergreen(path):
        return score

    ref_date = _parse_dated_path(path)
    if ref_date is None and mtime is not None:
        ref_date = datetime.fromtimestamp(mtime, tz=UTC)

    if ref_date is None:
        return score

    now = datetime.now(tz=UTC)
    age_days = (now - ref_date).total_seconds() / 86400.0
    lam = math.log(2) / half_life_days
    return score * math.exp(-lam * age_days)


def _jaccard_similarity(a: str, b: str) -> float:
    """Token-level Jaccard similarity for MMR diversity."""

    def tokenize(text: str) -> set[str]:
        tokens: set[str] = set()
        # ASCII words
        tokens.update(re.findall(r"[a-zA-Z0-9]+", text.lower()))
        # CJK unigrams + bigrams
        cjk = re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]", text)
        tokens.update(cjk)
        for i in range(len(cjk) - 1):
            tokens.add(cjk[i] + cjk[i + 1])
        return tokens

    ta, tb = tokenize(a), tokenize(b)
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)


def _mmr_rerank(
    results: list[MemorySearchResult],
    lam: float = 0.7,
    k: int = 10,
) -> list[MemorySearchResult]:
    """Maximal Marginal Relevance re-ranking for diversity."""
    if len(results) <= 1:
        return results[:k]

    # Normalize scores to [0,1] via local dict (avoid mutating caller's objects)
    max_score = max(r.score for r in results)
    norm: dict[int, float] = {id(r): r.score / max_score if max_score > 0 else 0.0 for r in results}

    selected: list[MemorySearchResult] = []
    remaining = list(results)

    while remaining and len(selected) < k:
        if not selected:
            best = max(remaining, key=lambda r: norm[id(r)])
        else:

            def mmr_score(candidate: MemorySearchResult) -> float:
                max_sim = max(_jaccard_similarity(candidate.snippet, s.snippet) for s in selected)
                return lam * norm[id(candidate)] - (1 - lam) * max_sim

            best = max(remaining, key=mmr_score)

        selected.append(best)
        remaining.remove(best)

    return selected


def _copy_result_with_score(result: MemorySearchResult, score: float) -> MemorySearchResult:
    return MemorySearchResult(
        chunk_id=result.chunk_id,
        path=result.path,
        source=result.source,
        start_line=result.start_line,
        end_line=result.end_line,
        snippet=result.snippet,
        score=score,
        vector_score=result.vector_score,
        text_score=result.text_score,
        text=result.text,
        chunk_hash=result.chunk_hash,
        metadata=dict(result.metadata),
        citation=result.citation,
    )


def _rank_score(
    result: MemorySearchResult,
    source_weights: dict[MemorySource, float],
) -> float:
    return result.score * source_weights.get(result.source, 1.0)


class MemoryRetriever:
    """
    Unified retrieval interface that wraps LongTermMemoryStore.
    Supports:
    - Hybrid search (vector + FTS5)
    - Optional temporal decay
    - Optional MMR re-ranking
    - Time-range filtering via date constraints
    """

    def __init__(
        self,
        store: LongTermMemoryStore,
        temporal_decay_enabled: bool = False,
        temporal_decay_half_life_days: float = 30.0,
        mmr_enabled: bool = False,
        mmr_lambda: float = 0.7,
        vector_weight: float = 0.7,
        text_weight: float = 0.3,
        source_weights: dict[MemorySource, float] | None = None,
        sync_manager: Any | None = None,
        effective_metadata: dict[str, str] | None = None,
    ) -> None:
        self._store = store
        self._temporal_decay_enabled = temporal_decay_enabled
        self._temporal_decay_half_life_days = temporal_decay_half_life_days
        self._mmr_enabled = mmr_enabled
        self._mmr_lambda = mmr_lambda
        self._vector_weight = vector_weight
        self._text_weight = text_weight
        self._source_weights = source_weights or {MemorySource.sessions: 0.92}
        self._sync_manager = sync_manager
        self._effective_metadata = dict(effective_metadata or {})

    async def search(
        self,
        query: str,
        opts: MemorySearchOpts | None = None,
        *,
        intent: SearchIntent = SearchIntent.TOOL,
    ) -> list[MemorySearchResult]:
        if self._sync_manager is not None:
            await self._sync_manager.sync(reason=f"search:{intent.value}")
        opts = opts or MemorySearchOpts()
        source_filter = getattr(opts, "source", None)

        raw_results, _mode = await self._store.search(
            query=query,
            max_results=min(200, opts.max_results * 10),
            min_score=opts.min_score,
            vector_weight=self._vector_weight,
            text_weight=self._text_weight,
            source=source_filter,
        )

        if self._temporal_decay_enabled:
            unique_paths = list({r.path for r in raw_results})
            mtimes = await self._store.get_file_mtimes(unique_paths)
            raw_results = [
                MemorySearchResult(
                    chunk_id=r.chunk_id,
                    path=r.path,
                    source=r.source,
                    start_line=r.start_line,
                    end_line=r.end_line,
                    snippet=r.snippet,
                    score=_temporal_decay(
                        r.score,
                        r.path,
                        mtimes.get(r.path),
                        self._temporal_decay_half_life_days,
                    ),
                    vector_score=r.vector_score,
                    text_score=r.text_score,
                    text=r.text,
                    chunk_hash=r.chunk_hash,
                    metadata=dict(r.metadata),
                    citation=r.citation,
                )
                for r in raw_results
            ]
            raw_results.sort(key=lambda r: r.score, reverse=True)

        # Filter by min_score before optional diversity selection. Store-level
        # recall guarantees deliberately survive this second pass.
        filtered = [
            r
            for r in raw_results
            if is_searchable_source_path(r.source, str(r.path))
            and (source_filter is None or r.source == source_filter)
            and (
                r.score >= opts.min_score
                or is_relaxed_keyword_match(r)
                or is_lexical_guaranteed_match(r)
            )
        ]
        filtered.sort(key=lambda r: _rank_score(r, self._source_weights), reverse=True)

        if self._mmr_enabled:
            weighted = [
                _copy_result_with_score(r, _rank_score(r, self._source_weights))
                for r in filtered
            ]
            selected_weighted = _mmr_rerank(
                weighted,
                lam=self._mmr_lambda,
                k=opts.max_results,
            )
            original_by_chunk = {r.chunk_id: r for r in filtered}
            k_selected = [original_by_chunk[r.chunk_id] for r in selected_weighted]
        else:
            k_selected = filtered[: opts.max_results]
        for result in k_selected:
            result.metadata["search_intent"] = intent.value
        return k_selected

    async def close(self) -> None:
        return None

    def effective_retrieval_metadata(self) -> dict[str, str]:
        effective_mode = "fts_only" if self._vector_weight == 0.0 else "hybrid"
        metadata = {
            "retrieval_mode": effective_mode,
            "vector_weight": str(self._vector_weight),
            "text_weight": str(self._text_weight),
        }
        metadata.update(self._effective_metadata)
        return metadata
