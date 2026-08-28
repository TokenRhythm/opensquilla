"""Semantic ranking layer: encode skills once, cosine-rank queries."""

from __future__ import annotations

import numpy as np
import structlog

from opensquilla.skills.retrieval.lexical import (
    Hit,
    _skill_cache_fingerprint,
    _skill_id,
    _stringify,
)
from opensquilla.skills.types import SkillSpec

log = structlog.get_logger(__name__)


def _skill_text(skill: SkillSpec) -> str:
    # Defensive stringify: malformed YAML frontmatter can produce
    # list/dict values where a string is expected. See lexical._stringify.
    return (
        f"{_stringify(skill.name)}\n{_stringify(skill.description)}\n{_stringify(skill.triggers)}"
    )


class SemanticIndex:
    """Encodes a fixed skill set into a (N, dim) matrix; ranks queries
    by cosine similarity. Holding the matrix in memory is cheap (~90 KB
    for ~44 skills × 512 dims × 4 B); hot reload triggers a rebuild via
    HybridRetriever fingerprinting."""

    def __init__(self, embedder) -> None:  # type: ignore[no-untyped-def]
        self._embedder = embedder
        self._matrix: np.ndarray | None = None
        self._ids: list[str] = []
        self._fingerprint: tuple[str, ...] | None = None

    def _fp(self, skills: list[SkillSpec]) -> tuple[str, ...]:
        return tuple(_skill_cache_fingerprint(s) for s in skills)

    def build(self, skills: list[SkillSpec]) -> None:
        """Encode the skill set. Idempotent on the retrieval-content fingerprint.

        ALL embedder exceptions propagate. HybridRetriever decides
        whether to permanently disable the semantic path
        (ImportError/OSError) or retry next turn (everything else).
        On any exception this method clears its internal matrix so
        no stale ranks are emitted before the caller's decision."""
        fp = self._fp(skills)
        if fp == self._fingerprint and self._matrix is not None:
            return  # idempotent: same set, no work
        if not skills:
            self._matrix = np.zeros((0, 0), dtype=np.float32)
            self._ids = []
            self._fingerprint = fp
            return
        texts = [_skill_text(s) for s in skills]
        try:
            mat = self._embedder.encode_sync(texts)
        except Exception:
            # Clear state so any stale ranks are not emitted, then
            # propagate so the caller (HybridRetriever) classifies
            # permanent vs transient and decides whether to disable
            # or retry.
            self._matrix = None
            self._ids = []
            self._fingerprint = None
            raise
        # L2-normalise for cosine via dot product.
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._matrix = (mat / norms).astype(np.float32)
        self._ids = [_skill_id(skill) for skill in skills]
        self._fingerprint = fp

    def rank(self, query: str, top_n: int = 20) -> list[Hit]:
        """Rank skills by cosine similarity.

        ALL embedder exceptions propagate. Callers (HybridRetriever)
        decide whether the failure is permanent (ImportError/OSError —
        disable the semantic path) or transient (everything else —
        retry next turn). Swallowing exceptions here is wrong because
        the empty list returned for "encode raised" is indistinguishable
        from "encode succeeded with no positive cosine matches", and
        the two cases need different fallback behaviour."""
        if self._matrix is None or self._matrix.shape[0] == 0:
            return []
        if not query or not query.strip():
            return []
        qvec = self._embedder.encode_sync([query])[0]
        norm = float(np.linalg.norm(qvec)) or 1.0
        qvec = (qvec / norm).astype(np.float32)
        sims = self._matrix @ qvec  # (N,)
        order = np.argsort(-sims)[:top_n]
        # Drop zero/negative cosine — orthogonal or anti-parallel means no
        # semantic signal. Without this filter a degenerate or stub embedder
        # surfaces "matches" that break the both-layers-empty contract in
        # HybridRetriever.retrieve.
        hits: list[Hit] = []
        rank = 1
        for i in order:
            score = float(sims[int(i)])
            if score <= 0.0:
                continue
            hits.append(Hit(skill_id=self._ids[int(i)], rank=rank, score=score))
            rank += 1
        return hits

    def invalidate(self) -> None:
        self._matrix = None
        self._ids = []
        self._fingerprint = None
