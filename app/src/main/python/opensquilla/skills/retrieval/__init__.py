"""Skill retrieval — hybrid lexical + semantic ranking."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Literal

import structlog

from opensquilla.skills.retrieval.embedder import get_embedder
from opensquilla.skills.retrieval.fusion import rrf
from opensquilla.skills.retrieval.lexical import (
    LexicalIndex,
    _skill_cache_fingerprint,
    _skill_id,
)
from opensquilla.skills.types import SkillSpec

log = structlog.get_logger(__name__)

__all__ = ["HybridRetriever", "get_embedder"]

Strategy = Literal["lexical", "semantic", "hybrid"]

if TYPE_CHECKING:
    from opensquilla.skills.retrieval.semantic import SemanticIndex


class HybridRetriever:
    """Composes LexicalIndex + SemanticIndex via Reciprocal Rank Fusion.

    The `strategy` argument selects which ranking sources contribute:
    "lexical" runs FTS only and never touches the embedder; "semantic"
    runs cosine ranking only when an embedder is available, and falls
    back to lexical-only when the local embedding backend is unavailable
    (per spec §5.1 — silent degradation is preferred over an empty skill
    set for the turn); "hybrid" runs both and fuses via RRF.

    Indexes are rebuilt automatically when the input skill set changes
    (fingerprinted by retrieval-relevant metadata). Retrievals never raise; if the
    semantic path is unavailable (embedding backend missing or model
    load failure), the retriever degrades to lexical-only and stays
    that way for the rest of the process lifetime."""

    def __init__(
        self,
        embedder=None,  # type: ignore[no-untyped-def]
        rrf_k: int = 60,
        lexical_top_n: int = 20,
        semantic_top_n: int = 20,
        strategy: Strategy = "hybrid",
    ) -> None:
        self._rrf_k = rrf_k
        self._lex_top_n = lexical_top_n
        self._sem_top_n = semantic_top_n
        self._strategy: Strategy = strategy
        self._lock = threading.Lock()
        # Lexical and semantic indexes are fingerprinted independently:
        # a transient semantic build failure must not poison lexical's
        # cached state, and the next retrieve on the same skill set
        # must still re-attempt the semantic build.
        self._lexical_fp: tuple[str, ...] | None = None
        self._semantic_fp: tuple[str, ...] | None = None
        self._lexical: LexicalIndex | None = None
        self._semantic: SemanticIndex | None = None
        self._semantic_disabled = False
        self._semantic_unavailable_logged = False
        # strategy="lexical" never needs the embedder — do not even attempt
        # to construct it, so a missing extra is not a noisy import error.
        if strategy != "lexical":
            if embedder is not None:
                self._semantic = self._make_semantic_index(embedder)
            else:
                try:
                    self._semantic = self._make_semantic_index(get_embedder())
                except ImportError as exc:
                    self._semantic_disabled = True
                    self._log_semantic_unavailable(str(exc))

    def _make_semantic_index(self, embedder) -> SemanticIndex:  # type: ignore[no-untyped-def]
        from opensquilla.skills.retrieval.semantic import SemanticIndex

        return SemanticIndex(embedder)

    def _log_semantic_unavailable(self, error: str) -> None:
        if self._semantic_unavailable_logged:
            return
        log.warning("skills.retrieval.semantic_unavailable", error=error)
        self._semantic_unavailable_logged = True

    def invalidate(self) -> None:
        with self._lock:
            self._lexical_fp = None
            self._semantic_fp = None
            self._lexical = None
            if self._semantic is not None:
                self._semantic.invalidate()

    def _ensure_indexes(self, skills: list[SkillSpec]) -> bool:
        """Build/refresh indexes for the given skill set.

        Returns True if the semantic index is ready to serve rank()
        calls for this skill set. False means caller should treat
        semantic as failed-this-turn (lexical fallback under
        strategy="semantic"). When False is returned for a transient
        failure, _semantic_fp stays stale so the NEXT retrieve on the
        same skill set re-attempts the build."""
        fp = tuple(_skill_cache_fingerprint(s) for s in skills)
        with self._lock:
            # Lexical: always build on fingerprint mismatch. FTS table
            # population for ~44 skills is sub-millisecond, and even
            # strategy="semantic" needs it for fallback (spec §5.1).
            if self._lexical_fp != fp:
                self._lexical = LexicalIndex(skills)
                self._lexical_fp = fp

            # Semantic: build only when not permanently disabled.
            if self._semantic is None or self._semantic_disabled:
                return False

            if self._semantic_fp == fp:
                return True  # already built for this skill set

            try:
                self._semantic.build(skills)
            except (ImportError, OSError) as exc:
                # Permanent: skill-filter extra missing or model
                # weights unavailable. Disable semantic path for the
                # rest of the process so retrieve() degrades to
                # lexical-only.
                log.error("skills.retrieval.model_load_failed", error=str(exc))
                self._semantic_disabled = True
                self._log_semantic_unavailable(str(exc))
                return False
            except Exception as exc:
                # Transient: log and leave _semantic_fp untouched so
                # the next retrieve on this skill set re-attempts.
                log.warning("skills.retrieval.build_failed", error=str(exc))
                return False
            self._semantic_fp = fp
            return True

    def retrieve(
        self,
        skills: list[SkillSpec],
        query: str,
        top_k: int = 5,
    ) -> list[SkillSpec]:
        if not skills:
            return []
        if not query or not query.strip():
            return list(skills[:top_k])

        semantic_ready = self._ensure_indexes(skills)

        # ── select active ranking sources by strategy ──
        # Per spec §5.1, "semantic" with a missing extra falls through
        # to lexical-only — silent degradation beats an empty turn.
        use_lexical: bool
        use_semantic: bool
        if self._strategy == "lexical":
            use_lexical, use_semantic = True, False
        elif self._strategy == "semantic":
            # Build-time failure (semantic_ready=False) for permanent
            # OR transient reasons → run lexical this turn. Permanent
            # also flips _semantic_disabled so future turns stay on
            # lexical without re-trying.
            if not semantic_ready:
                use_lexical, use_semantic = True, False
            else:
                use_lexical, use_semantic = False, True
        else:  # "hybrid"
            use_lexical = True
            use_semantic = semantic_ready

        rankings = []
        if use_lexical and self._lexical is not None:
            rankings.append(self._lexical.rank(query, top_n=self._lex_top_n))

        # Semantic rank: distinguish three outcomes for the fallback
        # decision (sem_failed True == ran but raised; False == ran
        # cleanly; None == we never called rank). A successful empty
        # result is NOT a failure — strategy="semantic" with no
        # cosine matches must respect §6's "both layers empty → []"
        # rather than silently inject lexical hits the user did not ask
        # for.
        sem_failed: bool | None = None
        if use_semantic and self._semantic is not None:
            try:
                sem_hits = self._semantic.rank(query, top_n=self._sem_top_n)
                sem_failed = False
            except (ImportError, OSError) as exc:
                # Permanent: extra was uninstalled mid-flight, or model
                # storage went away. Disable semantic for future turns.
                log.error("skills.retrieval.encode_unavailable", error=str(exc))
                self._semantic_disabled = True
                self._log_semantic_unavailable(str(exc))
                sem_hits = []
                sem_failed = True
            except Exception as exc:
                # Transient: do NOT disable; next turn may recover.
                log.warning("skills.retrieval.encode_failed", error=str(exc))
                sem_hits = []
                sem_failed = True
            if sem_hits:
                rankings.append(sem_hits)

        # strategy="semantic" + rank call failed (permanent or transient)
        # → run lexical so the turn still has candidates. When semantic
        # succeeded with [] (genuine no-match), respect §6.
        if self._strategy == "semantic" and sem_failed is True and self._lexical is not None:
            rankings.append(self._lexical.rank(query, top_n=self._lex_top_n))

        # Drop empties so RRF doesn't see noise lists.
        rankings = [r for r in rankings if r]
        if not rankings:
            log.warning("skills.retrieval.full_failure", query_len=len(query))
            return []

        fused = rrf(rankings, k=self._rrf_k)
        keep_ids = {sid for sid, _ in fused[:top_k]}
        if not keep_ids:
            return []

        # Map fused ids back to SkillSpecs preserving fused order.
        by_id = {_skill_id(s): s for s in skills}
        out: list[SkillSpec] = []
        for sid, _score in fused[:top_k]:
            spec = by_id.get(sid)
            if spec is not None:
                out.append(spec)
        return out
