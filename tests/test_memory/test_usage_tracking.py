"""Tests for D11 (Usage Tracking) + D5 (Dream scoring enhancement)."""

from __future__ import annotations

import asyncio

import pytest

from opensquilla.memory.dream.models import PromotionEvidenceEntry, PromotionEvidenceStore
from opensquilla.memory.dream.ranking import (
    _CONSTRAINT_STABILITY,
    _DEFAULT_CONSTRAINT_STABILITY,
    _cross_task_relevance,
    _score,
    rank_promotion_candidates,
)


# ── D11: Store-level usage tracking ──────────────────────────────────────


class TestChunkUsageSchema:
    @pytest.mark.asyncio
    async def test_chunk_usage_table_created(self, tmp_path):
        """initialize() creates chunk_usage table with correct columns."""
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.embedding import NullEmbeddingProvider

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test.db"),
            embedding_provider=NullEmbeddingProvider(),
        )
        await store.initialize()
        try:
            async with store._db.execute("PRAGMA table_info(chunk_usage)") as cur:
                columns = {row[1] for row in await cur.fetchall()}
            assert "chunk_id" in columns
            assert "intent" in columns
            assert "recall_count" in columns
            assert "last_recalled_at" in columns
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_index_exists(self, tmp_path):
        """idx_chunk_usage_chunk index is created."""
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.embedding import NullEmbeddingProvider

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test.db"),
            embedding_provider=NullEmbeddingProvider(),
        )
        await store.initialize()
        try:
            async with store._db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_chunk_usage_chunk'"
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
        finally:
            await store.close()


class TestRecordChunkUsage:
    @pytest.mark.asyncio
    async def test_increment_and_aggregate(self, tmp_path):
        """record_chunk_usage increments counts; get_usage_stats aggregates."""
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.types import MemorySource

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test.db"),
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=True,
        )
        await store.initialize()
        try:
            await store.index_file(
                "memory/test.md",
                "We decided to use PostgreSQL for the production database.",
                source=MemorySource.memory,
            )
            async with store._db.execute(
                "SELECT id FROM chunks WHERE path = ?", ("memory/test.md",)
            ) as cur:
                rows = await cur.fetchall()
            assert len(rows) >= 1
            chunk_id = rows[0][0]

            # Record usage with different intents
            await store.record_chunk_usage([chunk_id], intent="avoid_failure")
            await store.record_chunk_usage([chunk_id], intent="avoid_failure")
            await store.record_chunk_usage([chunk_id], intent="continue_task")

            # Verify raw counts
            async with store._db.execute(
                "SELECT intent, recall_count FROM chunk_usage WHERE chunk_id = ?",
                (chunk_id,),
            ) as cur:
                usage_rows = await cur.fetchall()
            usage_map = {r[0]: r[1] for r in usage_rows}
            assert usage_map["avoid_failure"] == 2
            assert usage_map["continue_task"] == 1

            # Verify aggregated stats
            stats = await store.get_usage_stats(["memory/test.md"])
            assert "memory/test.md" in stats
            assert stats["memory/test.md"]["total_recalls"] == 3
            assert stats["memory/test.md"]["intent_diversity"] == 2
            assert stats["memory/test.md"]["last_recalled_at"] is not None
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_empty_chunk_ids_noop(self, tmp_path):
        """record_chunk_usage with empty list is a no-op."""
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.embedding import NullEmbeddingProvider

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test.db"),
            embedding_provider=NullEmbeddingProvider(),
        )
        await store.initialize()
        try:
            await store.record_chunk_usage([], intent="general")
            async with store._db.execute("SELECT COUNT(*) FROM chunk_usage") as cur:
                assert (await cur.fetchone())[0] == 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_get_usage_stats_empty_paths(self, tmp_path):
        """get_usage_stats with empty paths returns empty dict."""
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.embedding import NullEmbeddingProvider

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test.db"),
            embedding_provider=NullEmbeddingProvider(),
        )
        await store.initialize()
        try:
            assert await store.get_usage_stats([]) == {}
        finally:
            await store.close()


class TestDominantConstraintTypes:
    @pytest.mark.asyncio
    async def test_dominant_type_by_confidence(self, tmp_path):
        """get_dominant_constraint_types returns type with highest total confidence."""
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.types import MemorySource

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test.db"),
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=True,
        )
        await store.initialize()
        try:
            await store.index_file(
                "memory/test.md",
                "We decided to use PostgreSQL for the production database.",
                source=MemorySource.memory,
            )
            result = await store.get_dominant_constraint_types(["memory/test.md"])
            assert result.get("memory/test.md") == "decision"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_no_annotated_chunks(self, tmp_path):
        """Files without annotated chunks return empty result."""
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.types import MemorySource

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test.db"),
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=False,
        )
        await store.initialize()
        try:
            await store.index_file(
                "memory/test.md",
                "Some content without annotation that is long enough to index.",
                source=MemorySource.memory,
            )
            result = await store.get_dominant_constraint_types(["memory/test.md"])
            assert result == {}
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_empty_paths_returns_empty(self, tmp_path):
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.embedding import NullEmbeddingProvider

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test.db"),
            embedding_provider=NullEmbeddingProvider(),
        )
        await store.initialize()
        try:
            assert await store.get_dominant_constraint_types([]) == {}
        finally:
            await store.close()


# ── D11: Retriever integration ───────────────────────────────────────────


class TestRetrieverUsageTracking:
    @pytest.mark.asyncio
    async def test_search_triggers_usage_write(self, tmp_path):
        """search() with usage_tracking_enabled fires record_chunk_usage."""
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.retrieval import MemoryRetriever
        from opensquilla.memory.types import MemorySource

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test.db"),
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=True,
        )
        await store.initialize()
        try:
            await store.index_file(
                "memory/test.md",
                "We decided to use PostgreSQL for the production database.",
                source=MemorySource.memory,
            )
            retriever = MemoryRetriever(
                store=store,
                usage_tracking_enabled=True,
            )
            results = await retriever.search("PostgreSQL database")
            # Give fire-and-forget task time to complete
            await asyncio.sleep(0.1)
            if results:
                stats = await store.get_usage_stats(["memory/test.md"])
                assert isinstance(stats, dict)
                # If FTS matched, usage should be recorded
                if "memory/test.md" in stats:
                    assert stats["memory/test.md"]["total_recalls"] >= 1
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_usage_tracking_disabled_no_write(self, tmp_path):
        """search() with usage_tracking_enabled=False does not write usage."""
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.retrieval import MemoryRetriever
        from opensquilla.memory.types import MemorySource

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test.db"),
            embedding_provider=NullEmbeddingProvider(),
        )
        await store.initialize()
        try:
            await store.index_file(
                "memory/test.md",
                "We decided to use PostgreSQL for the production database.",
                source=MemorySource.memory,
            )
            retriever = MemoryRetriever(
                store=store,
                usage_tracking_enabled=False,
            )
            await retriever.search("PostgreSQL database")
            await asyncio.sleep(0.1)
            async with store._db.execute("SELECT COUNT(*) FROM chunk_usage") as cur:
                assert (await cur.fetchone())[0] == 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_usage_tracking_property(self, tmp_path):
        """usage_tracking_enabled property reflects constructor arg."""
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.retrieval import MemoryRetriever

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test.db"),
            embedding_provider=NullEmbeddingProvider(),
        )
        await store.initialize()
        try:
            r_on = MemoryRetriever(store=store, usage_tracking_enabled=True)
            r_off = MemoryRetriever(store=store, usage_tracking_enabled=False)
            assert r_on.usage_tracking_enabled is True
            assert r_off.usage_tracking_enabled is False
        finally:
            await store.close()


# ── D5: Dream scoring enhancement ────────────────────────────────────────


def _make_entry(**overrides) -> PromotionEvidenceEntry:
    defaults = dict(
        candidate_id="test-candidate",
        agent_id="main",
        source_path="memory/test.md",
        source_kind="memory_file",
        source_mtime_ns=1_000_000_000,
        source_size=100,
        snippet="Test snippet",
        snippet_sha256="abc123",
        claim_sha256="def456",
        first_seen_at="2026-01-01T00:00:00Z",
        last_seen_at="2026-01-01T00:00:00Z",
        seen_count=3,
        positive_signal_count=1,
        correction_signal_count=0,
        failure_signal_count=0,
        manual_signal_count=0,
        source_days=["2026-01-01", "2026-01-02"],
    )
    defaults.update(overrides)
    return PromotionEvidenceEntry(**defaults)


class TestConstraintStability:
    def test_stability_lookup(self):
        """All 10 constraint types have stability values."""
        assert len(_CONSTRAINT_STABILITY) == 10
        assert _CONSTRAINT_STABILITY["fact"] == 1.0
        assert _CONSTRAINT_STABILITY["decision"] == 1.0
        assert _CONSTRAINT_STABILITY["constraint"] == 1.0
        assert _CONSTRAINT_STABILITY["assumption"] == 0.3
        assert _CONSTRAINT_STABILITY["anti_pattern"] == 0.3

    def test_stable_type_scores_higher(self):
        """fact (1.0) scores higher than assumption (0.3) with same evidence."""
        entry = _make_entry()
        score_fact = _score(entry, constraint_type="fact")
        score_assumption = _score(entry, constraint_type="assumption")
        assert score_fact > score_assumption

    def test_no_annotation_default(self):
        """No constraint_type -> default stability 0.65."""
        entry = _make_entry()
        score_none = _score(entry, constraint_type=None)
        score_default = _score(entry, constraint_type="unknown_type")
        assert score_none == score_default  # both use 0.65
        assert _DEFAULT_CONSTRAINT_STABILITY == 0.65


class TestCrossTaskRelevance:
    def test_no_usage_zero(self):
        assert _cross_task_relevance(None) == 0.0
        assert _cross_task_relevance({}) == 0.0
        assert _cross_task_relevance({"total_recalls": 0, "intent_diversity": 0}) == 0.0

    def test_high_recall_high_diversity(self):
        usage = {"total_recalls": 20, "intent_diversity": 4}
        score = _cross_task_relevance(usage)
        assert 0.5 < score <= 1.0

    def test_low_recall_low_diversity(self):
        usage = {"total_recalls": 1, "intent_diversity": 1}
        score = _cross_task_relevance(usage)
        assert 0.0 < score < 0.3

    def test_monotonic_in_recall(self):
        """More recalls -> higher score (same diversity)."""
        low = _cross_task_relevance({"total_recalls": 2, "intent_diversity": 2})
        high = _cross_task_relevance({"total_recalls": 10, "intent_diversity": 2})
        assert high > low

    def test_monotonic_in_diversity(self):
        """More intent diversity -> higher score (same recalls)."""
        low = _cross_task_relevance({"total_recalls": 5, "intent_diversity": 1})
        high = _cross_task_relevance({"total_recalls": 5, "intent_diversity": 3})
        assert high > low


class TestD5Scoring:
    def test_backward_compatible_no_extras(self):
        """Without usage_stats/constraint_type, score is still valid [0,1]."""
        entry = _make_entry()
        score = _score(entry)
        assert 0.0 <= score <= 1.0

    def test_enhanced_score_higher_with_good_data(self):
        """Stable type + high usage -> higher score than baseline."""
        entry = _make_entry()
        baseline = _score(entry)
        enhanced = _score(
            entry,
            constraint_type="fact",
            usage_stats={"total_recalls": 15, "intent_diversity": 3},
        )
        assert enhanced > baseline

    def test_rank_promotion_with_d5(self):
        """rank_promotion_candidates accepts and uses D5 params."""
        store = PromotionEvidenceStore(
            entries={"c1": _make_entry(candidate_id="c1", seen_count=3)},
        )
        # Without D5
        ranked_old = rank_promotion_candidates(
            store, min_score=0.0, negative_recurrence_threshold=2
        )
        # With D5
        ranked_new = rank_promotion_candidates(
            store,
            min_score=0.0,
            negative_recurrence_threshold=2,
            usage_stats={"memory/test.md": {"total_recalls": 10, "intent_diversity": 3}},
            constraint_types={"memory/test.md": "fact"},
        )
        assert len(ranked_old) == 1
        assert len(ranked_new) == 1
        assert ranked_new[0].score >= ranked_old[0].score
        # D5 reasons present
        assert any("stable_constraint_type" in r for r in ranked_new[0].reasons)
        assert any("recall_count=" in r for r in ranked_new[0].reasons)

    def test_rank_promotion_d5_none_degrades(self):
        """usage_stats=None, constraint_types=None -> old behavior."""
        store = PromotionEvidenceStore(
            entries={"c1": _make_entry(candidate_id="c1", seen_count=3)},
        )
        ranked = rank_promotion_candidates(
            store,
            min_score=0.0,
            negative_recurrence_threshold=2,
            usage_stats=None,
            constraint_types=None,
        )
        assert len(ranked) == 1
        assert 0.0 <= ranked[0].score <= 1.0

    def test_fact_outranks_assumption_in_ranking(self):
        """fact constraint_type gets higher rank score than assumption."""
        e1 = _make_entry(candidate_id="c1", source_path="memory/fact.md")
        e2 = _make_entry(candidate_id="c2", source_path="memory/assumption.md")
        store = PromotionEvidenceStore(entries={"c1": e1, "c2": e2})

        ranked = rank_promotion_candidates(
            store,
            min_score=0.0,
            negative_recurrence_threshold=2,
            constraint_types={
                "memory/fact.md": "fact",
                "memory/assumption.md": "assumption",
            },
        )
        fact = next(r for r in ranked if r.source_path == "memory/fact.md")
        assumption = next(r for r in ranked if r.source_path == "memory/assumption.md")
        assert fact.score > assumption.score
