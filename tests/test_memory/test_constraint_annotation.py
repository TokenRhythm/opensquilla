"""Tests for L1: Constraint Type Annotation.

Covers:
- Signal Gate (should_classify)
- Heuristic classification
- Frontmatter parsing (D6)
- CandidateKind -> ConstraintType mapping
- LLM classification (mocked)
- Store integration (schema + index_file annotation)
- Config integration
- Regression: flag off = no-op
"""

from __future__ import annotations

import pytest

from opensquilla.memory.constraint_classifier import (
    _alpha_cjk_ratio,
    classify_constraint_sync,
    heuristic_classify,
    llm_classify,
    parse_frontmatter_constraint,
    parse_inline_constraint,
    should_classify,
)
from opensquilla.memory.types import (
    CANDIDATE_KIND_TO_CONSTRAINT,
    CORE_CONSTRAINT_TYPES,
    ConstraintType,
)

# ── Signal Gate (D8) ──────────────────────────────────────────────────────


class TestSignalGate:
    def test_short_text_skipped(self):
        assert should_classify("hi") is False
        assert should_classify("ok") is False
        assert should_classify("a" * 19) is False

    def test_empty_text_skipped(self):
        assert should_classify("") is False

    def test_whitespace_only_skipped(self):
        assert should_classify("   \n\t  ") is False

    def test_heartbeat_skipped(self):
        assert should_classify("HEARTBEAT_OK") is False
        assert should_classify("NO_REPLY") is False

    def test_status_message_skipped(self):
        assert should_classify("ok") is False
        assert should_classify("done") is False
        assert should_classify("ready") is False

    def test_exit_code_skipped(self):
        assert should_classify("exit_code=0") is False

    def test_pure_tool_output_skipped(self):
        # Structured tool output patterns
        assert should_classify("12345 67890 exit_code=0 True") is False
        assert should_classify("exit_code=1 some error output here") is False
        # Pure numbers/symbols (low alpha ratio)
        assert should_classify("12345 67890 0x1F3A = 42 + 99") is False

    def test_normal_text_passes(self):
        assert should_classify("We decided to use SQLite for storage") is True

    def test_chinese_text_passes(self):
        assert should_classify("我们决定使用 SQLite 作为数据库存储方案") is True

    def test_medium_length_text_passes(self):
        assert should_classify("This is a valid chunk of text.") is True


class TestAlphaCjkRatio:
    def test_all_alpha(self):
        assert _alpha_cjk_ratio("hello world") == 1.0

    def test_all_cjk(self):
        assert _alpha_cjk_ratio("你好世界") == 1.0

    def test_mixed(self):
        ratio = _alpha_cjk_ratio("hello 123 !!!")
        # 5 alpha / 11 non-ws chars (hello=5, 123=3, !!!=3) = 0.4545...
        assert abs(ratio - 5.0 / 11.0) < 1e-9

    def test_empty(self):
        assert _alpha_cjk_ratio("") == 0.0


# ── Heuristic Classification ──────────────────────────────────────────────


class TestHeuristicClassify:
    def test_decision_keywords(self):
        ct, conf = heuristic_classify("We decided to use PostgreSQL")
        assert ct == ConstraintType.decision
        assert conf == 0.6

    def test_decision_chinese(self):
        ct, conf = heuristic_classify("我们选择了 SQLite")
        assert ct == ConstraintType.decision

    def test_preference_keywords(self):
        ct, conf = heuristic_classify("I prefer TypeScript over JavaScript")
        assert ct == ConstraintType.preference
        assert conf == 0.6

    def test_procedure_keywords(self):
        ct, conf = heuristic_classify("Step 1: install the package")
        assert ct == ConstraintType.procedure
        assert conf == 0.6

    def test_goal_keywords(self):
        ct, conf = heuristic_classify("Goal: complete the API refactor")
        assert ct == ConstraintType.goal
        assert conf == 0.5

    def test_event_keywords(self):
        ct, conf = heuristic_classify("Yesterday we deployed the fix")
        assert ct == ConstraintType.event
        assert conf == 0.5

    def test_event_chinese(self):
        ct, conf = heuristic_classify("昨天我们完成了重构工作")
        assert ct == ConstraintType.event

    def test_procedure_without_temporal(self):
        # Without temporal marker, "deploy" correctly triggers procedure
        ct, conf = heuristic_classify("To deploy the app, run the build script")
        assert ct == ConstraintType.procedure
        assert conf == 0.6

    def test_fact_default(self):
        ct, conf = heuristic_classify("Python 3.12 was released in 2023")
        assert ct == ConstraintType.fact
        assert conf == 0.4

    def test_first_match_wins(self):
        # "decided" -> decision before "plan" -> goal
        ct, _ = heuristic_classify("I decided to plan the next steps")
        assert ct == ConstraintType.decision

    def test_confidence_range(self):
        """All heuristic confidences should be in [0.4, 0.7]."""
        texts = [
            "We decided to use X",
            "I prefer Y",
            "How to install Z",
            "Our goal is W",
            "Yesterday we did V",
            "Random text with no keywords at all here",
        ]
        for text in texts:
            _, conf = heuristic_classify(text)
            assert 0.3 <= conf <= 0.7, f"confidence {conf} out of range for: {text}"


# ── Frontmatter Parsing (D6) ──────────────────────────────────────────────


class TestFrontmatterParsing:
    def test_valid_frontmatter(self):
        text = "---\nconstraint_type: procedure\n---\nDeploy steps here"
        result = parse_frontmatter_constraint(text)
        assert result == ConstraintType.procedure

    def test_no_frontmatter(self):
        assert parse_frontmatter_constraint("Plain text without frontmatter") is None

    def test_frontmatter_without_constraint_type(self):
        text = "---\ntitle: My Document\n---\ncontent"
        assert parse_frontmatter_constraint(text) is None

    def test_invalid_type(self):
        text = "---\nconstraint_type: invalid_type\n---\ncontent"
        assert parse_frontmatter_constraint(text) is None

    def test_case_insensitive(self):
        text = "---\nconstraint_type: DECISION\n---\ncontent"
        assert parse_frontmatter_constraint(text) == ConstraintType.decision

    def test_whitespace_handling(self):
        text = "---\nconstraint_type:  goal  \n---\ncontent"
        assert parse_frontmatter_constraint(text) == ConstraintType.goal

    def test_extended_type_accepted(self):
        """Extended types are valid in the enum (even if not active for v0.7)."""
        text = "---\nconstraint_type: pattern\n---\ncontent"
        assert parse_frontmatter_constraint(text) == ConstraintType.pattern


# ── Unified classify_constraint_sync ──────────────────────────────────────


class TestClassifyConstraintSync:
    def test_frontmatter_override(self):
        text = "---\nconstraint_type: procedure\n---\nI like to deploy this way"
        ct, conf = classify_constraint_sync(text)
        assert ct == ConstraintType.procedure
        assert conf == 1.0

    def test_frontmatter_overrides_keywords(self):
        # Even though "like" -> preference, frontmatter says decision
        text = "---\nconstraint_type: decision\n---\nI prefer TypeScript"
        ct, conf = classify_constraint_sync(text)
        assert ct == ConstraintType.decision
        assert conf == 1.0

    def test_signal_gate_skip(self):
        ct, conf = classify_constraint_sync("hi")
        assert ct == ConstraintType.fact
        assert conf is None

    def test_heuristic_fallback(self):
        ct, conf = classify_constraint_sync("We decided to use SQLite for storage")
        assert ct == ConstraintType.decision
        assert conf == 0.6

    def test_default_fact(self):
        ct, conf = classify_constraint_sync(
            "Python is a programming language that runs on many platforms"
        )
        assert ct == ConstraintType.fact
        assert conf == 0.4


# ── LLM Classification (mocked) ──────────────────────────────────────────


class TestLlmClassify:
    @pytest.mark.asyncio
    async def test_llm_success(self):
        async def mock_llm(prompt: str) -> str:
            return "decision"

        result = await llm_classify("We chose SQLite", mock_llm)
        assert result is not None
        ct, conf = result
        assert ct == ConstraintType.decision
        assert conf == 0.8

    @pytest.mark.asyncio
    async def test_llm_invalid_type(self):
        async def mock_llm(prompt: str) -> str:
            return "invalid_type"

        result = await llm_classify("Some text", mock_llm)
        assert result is None

    @pytest.mark.asyncio
    async def test_llm_extended_type_rejected(self):
        """LLM returning extended type (v0.8) should be rejected in v0.7."""

        async def mock_llm(prompt: str) -> str:
            return "anti_pattern"

        result = await llm_classify("Some text", mock_llm)
        assert result is None

    @pytest.mark.asyncio
    async def test_llm_failure_returns_none(self):
        async def mock_llm(prompt: str) -> str:
            raise RuntimeError("LLM unavailable")

        result = await llm_classify("Some text", mock_llm)
        assert result is None

    @pytest.mark.asyncio
    async def test_llm_extra_whitespace(self):
        async def mock_llm(prompt: str) -> str:
            return "  procedure  \n"

        result = await llm_classify("Some text", mock_llm)
        assert result is not None
        assert result[0] == ConstraintType.procedure


# ── CandidateKind -> ConstraintType mapping ───────────────────────────────


class TestCandidateKindMapping:
    def test_all_core_kinds_mapped(self):
        expected = {
            "fact": ConstraintType.fact,
            "event": ConstraintType.event,
            "preference": ConstraintType.preference,
            "decision": ConstraintType.decision,
            "procedure": ConstraintType.procedure,
            "todo": ConstraintType.goal,
            "goal": ConstraintType.goal,
        }
        assert CANDIDATE_KIND_TO_CONSTRAINT == expected

    def test_todo_maps_to_goal(self):
        assert CANDIDATE_KIND_TO_CONSTRAINT["todo"] == ConstraintType.goal

    def test_all_core_types_in_frozenset(self):
        expected = frozenset({
            ConstraintType.fact,
            ConstraintType.event,
            ConstraintType.preference,
            ConstraintType.decision,
            ConstraintType.procedure,
            ConstraintType.goal,
        })
        assert CORE_CONSTRAINT_TYPES == expected

    def test_extended_types_not_in_core(self):
        assert ConstraintType.assumption not in CORE_CONSTRAINT_TYPES
        assert ConstraintType.constraint not in CORE_CONSTRAINT_TYPES
        assert ConstraintType.anti_pattern not in CORE_CONSTRAINT_TYPES
        assert ConstraintType.pattern not in CORE_CONSTRAINT_TYPES


# ── Store integration (schema + index_file) ──────────────────────────────


class TestStoreSchemaMigration:
    @pytest.mark.asyncio
    async def test_constraint_columns_added(self, tmp_path):
        """initialize() idempotently adds constraint_type and constraint_confidence."""
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.store import LongTermMemoryStore

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test_memory.db"),
            embedding_provider=NullEmbeddingProvider(),
        )
        await store.initialize()
        try:
            async with store._db.execute("PRAGMA table_info(chunks)") as cur:
                columns = {row[1] for row in await cur.fetchall()}
            assert "constraint_type" in columns
            assert "constraint_confidence" in columns
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_constraint_columns_idempotent(self, tmp_path):
        """Calling initialize() twice doesn't error."""
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.store import LongTermMemoryStore

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test_memory.db"),
            embedding_provider=NullEmbeddingProvider(),
        )
        await store.initialize()
        await store.close()

        store2 = LongTermMemoryStore(
            db_path=str(tmp_path / "test_memory.db"),
            embedding_provider=NullEmbeddingProvider(),
        )
        await store2.initialize()
        try:
            async with store2._db.execute("PRAGMA table_info(chunks)") as cur:
                columns = {row[1] for row in await cur.fetchall()}
            assert "constraint_type" in columns
            assert "constraint_confidence" in columns
        finally:
            await store2.close()


class TestStoreIndexFileAnnotation:
    @pytest.mark.asyncio
    async def test_annotation_off_default(self, tmp_path):
        """With annotation off, chunks get DEFAULT 'fact' and NULL confidence."""
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.types import MemorySource

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test_memory.db"),
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=False,
        )
        await store.initialize()
        try:
            await store.index_file(
                "memory/test.md",
                "We decided to use SQLite for the database layer.",
                source=MemorySource.memory,
            )
            async with store._db.execute(
                "SELECT constraint_type, constraint_confidence FROM chunks WHERE path = ?",
                ("memory/test.md",),
            ) as cur:
                rows = await cur.fetchall()
            assert len(rows) == 1
            # Default 'fact' from schema, NULL confidence (not annotated)
            assert rows[0][0] == "fact"
            assert rows[0][1] is None
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_annotation_on_classifies(self, tmp_path):
        """With annotation on, chunks get heuristic classification."""
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.types import MemorySource

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test_memory.db"),
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
                "SELECT constraint_type, constraint_confidence FROM chunks WHERE path = ?",
                ("memory/test.md",),
            ) as cur:
                rows = await cur.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "decision"
            assert rows[0][1] is not None
            assert 0.3 <= rows[0][1] <= 0.7
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_frontmatter_override_in_store(self, tmp_path):
        """Frontmatter constraint_type overrides heuristic classification."""
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.types import MemorySource

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test_memory.db"),
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=True,
        )
        await store.initialize()
        try:
            content = (
                "---\n"
                "constraint_type: procedure\n"
                "---\n"
                "We decided to deploy with Docker."
            )
            await store.index_file(
                "memory/test.md",
                content,
                source=MemorySource.memory,
            )
            async with store._db.execute(
                "SELECT constraint_type, constraint_confidence FROM chunks WHERE path = ?",
                ("memory/test.md",),
            ) as cur:
                rows = await cur.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "procedure"
            assert rows[0][1] == 1.0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_frontmatter_override_applies_to_every_chunk(self, tmp_path):
        """A file-level override must survive chunking and annotate all content."""
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.types import MemorySource

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test_memory.db"),
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=True,
        )
        await store.initialize()
        try:
            content = (
                "---\nconstraint_type: procedure\n---\n"
                + "\n".join(
                    f"Step {index}: perform the deployment action carefully."
                    for index in range(30)
                )
            )
            count = await store.index_file(
                "memory/long.md",
                content,
                source=MemorySource.memory,
                chunk_tokens=20,
                chunk_overlap=0,
            )
            assert count > 1
            async with store._db.execute(
                "SELECT constraint_type, constraint_confidence "
                "FROM chunks WHERE path = ?",
                ("memory/long.md",),
            ) as cur:
                rows = await cur.fetchall()
            assert rows
            assert all(row[0] == "procedure" and row[1] == 1.0 for row in rows)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_enabling_annotation_reindexes_unchanged_files(self, tmp_path):
        """Turning L1 on must not strand an existing unannotated index."""
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.types import MemorySource

        db_path = str(tmp_path / "test_memory.db")
        content = "We decided to use PostgreSQL for production."
        disabled = LongTermMemoryStore(
            db_path=db_path,
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=False,
        )
        await disabled.initialize()
        await disabled.index_file(
            "memory/test.md", content, source=MemorySource.memory
        )
        await disabled.close()

        enabled = LongTermMemoryStore(
            db_path=db_path,
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=True,
        )
        await enabled.initialize()
        try:
            count = await enabled.index_file(
                "memory/test.md", content, source=MemorySource.memory
            )
            assert count == 1
            async with enabled._db.execute(
                "SELECT constraint_type FROM chunks WHERE path = ?",
                ("memory/test.md",),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == "decision"
        finally:
            await enabled.close()

    @pytest.mark.asyncio
    async def test_annotation_off_no_regression(self, tmp_path):
        """With annotation off, index_file behaves exactly as before."""
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.types import MemorySource

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test_memory.db"),
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=False,
        )
        await store.initialize()
        try:
            count = await store.index_file(
                "memory/test.md",
                "This is a test memory chunk with enough length.",
                source=MemorySource.memory,
            )
            assert count >= 1
        finally:
            await store.close()


# ── Config integration ────────────────────────────────────────────────────


class TestConfigIntegration:
    def test_experimental_config_defaults(self):
        from opensquilla.gateway.config import MemoryExperimentalConfig

        cfg = MemoryExperimentalConfig()
        assert cfg.constraint_annotation is False
        assert cfg.constraint_routing is False
        assert cfg.sufficiency_check is False

    def test_memory_config_has_experimental(self):
        from opensquilla.gateway.config import MemoryConfig

        cfg = MemoryConfig()
        assert hasattr(cfg, "experimental")
        assert cfg.experimental.constraint_annotation is False

    def test_experimental_config_can_enable(self):
        from opensquilla.gateway.config import MemoryExperimentalConfig

        cfg = MemoryExperimentalConfig(constraint_annotation=True)
        assert cfg.constraint_annotation is True


# ── A1: Tiered Escalation Pipeline ──────────────────────────────────────────


class TestA1TieredEscalation:
    """A1: heuristic-first with LLM escalation for low-confidence chunks."""

    def test_heuristic_high_confidence_accepted_directly(self):
        """Heuristic confidence >= 0.6 should be accepted without LLM call."""
        from opensquilla.memory.constraint_classifier import (
            _HEURISTIC_ACCEPT_THRESHOLD,
            heuristic_classify,
        )
        # "decided" → decision, confidence 0.6
        h_type, h_conf = heuristic_classify("we decided to use Python for the backend")
        assert h_type == ConstraintType.decision
        assert h_conf >= _HEURISTIC_ACCEPT_THRESHOLD

    def test_heuristic_low_confidence_below_threshold(self):
        """Heuristic confidence < 0.6 should NOT be treated as final."""
        from opensquilla.memory.constraint_classifier import (
            _HEURISTIC_ACCEPT_THRESHOLD,
            heuristic_classify,
        )
        # "yesterday" → event, confidence 0.5 (below threshold)
        h_type, h_conf = heuristic_classify("yesterday we had a long meeting about architecture")
        assert h_type == ConstraintType.event
        assert h_conf < _HEURISTIC_ACCEPT_THRESHOLD

    def test_threshold_consistent_with_l2(self):
        """A1 _HEURISTIC_ACCEPT_THRESHOLD must equal L2 CONFIDENCE_THRESHOLD."""
        from opensquilla.memory.constraint_classifier import _HEURISTIC_ACCEPT_THRESHOLD
        from opensquilla.memory.constraint_routing import CONFIDENCE_THRESHOLD
        assert _HEURISTIC_ACCEPT_THRESHOLD == CONFIDENCE_THRESHOLD

    @pytest.mark.asyncio
    async def test_high_confidence_skips_llm(self):
        """LLM call should NOT be invoked when heuristic confidence >= 0.6."""
        from opensquilla.memory.constraint_classifier import classify_constraint

        llm_called = False

        async def fake_llm(prompt: str) -> str:
            nonlocal llm_called
            llm_called = True
            return "event"

        # "决定" → decision, 0.6 >= threshold → LLM skipped
        text = "我们经过充分讨论后决定用 Python 做后端开发框架"
        ct, conf = await classify_constraint(text, llm_call=fake_llm)
        assert ct == ConstraintType.decision
        assert conf == 0.6
        assert not llm_called, "LLM should NOT be called when heuristic confidence is high"

    @pytest.mark.asyncio
    async def test_low_confidence_triggers_llm(self):
        """LLM call should be invoked when heuristic confidence < 0.6."""
        from opensquilla.memory.constraint_classifier import classify_constraint

        llm_called = False

        async def fake_llm(prompt: str) -> str:
            nonlocal llm_called
            llm_called = True
            return "event"

        # "昨天" → event, 0.5 < threshold → LLM called
        text = "昨天下午开会讨论了项目架构设计方案和技术选型"
        ct, conf = await classify_constraint(text, llm_call=fake_llm)
        assert llm_called, "LLM should be called when heuristic confidence is low"
        assert ct == ConstraintType.event
        assert conf == 0.8  # LLM confidence (_LLM_CONFIDENCE)

    @pytest.mark.asyncio
    async def test_llm_unavailable_falls_back_to_heuristic(self):
        """When LLM is not available, low-confidence heuristic result is returned."""
        from opensquilla.memory.constraint_classifier import classify_constraint

        text = "昨天下午开会讨论了项目架构设计方案和技术选型"
        ct, conf = await classify_constraint(text, llm_call=None)
        assert ct == ConstraintType.event
        assert conf == 0.5  # heuristic confidence

    @pytest.mark.asyncio
    async def test_llm_parse_failure_falls_back(self):
        """When LLM returns unparseable response, fall back to heuristic."""
        from opensquilla.memory.constraint_classifier import classify_constraint

        async def failing_llm(prompt: str) -> str:
            return "bogus_response_that_wont_parse_to_any_type"

        text = "昨天下午开会讨论了项目架构设计方案和技术选型"
        ct, conf = await classify_constraint(text, llm_call=failing_llm)
        assert ct == ConstraintType.event
        assert conf == 0.5

    @pytest.mark.asyncio
    async def test_inline_marker_bypasses_heuristic_and_llm(self):
        """Inline marker (B4 path) should be resolved before heuristic/LLM."""
        from opensquilla.memory.constraint_classifier import classify_constraint

        llm_called = False

        async def fake_llm(prompt: str) -> str:
            nonlocal llm_called
            llm_called = True
            return "fact"

        text = "<!-- opensquilla-constraint: decision --> we discussed something yesterday"
        ct, conf = await classify_constraint(text, llm_call=fake_llm)
        assert ct == ConstraintType.decision
        assert conf == 0.9
        assert not llm_called, "Inline marker should bypass both heuristic and LLM"

    def test_repeated_unanimous_inline_markers_are_a_chunk_override(self):
        text = (
            "<!-- opensquilla-constraint: decision --> chose SQLite\n"
            "<!-- opensquilla-constraint: decision --> kept WAL enabled"
        )
        assert parse_inline_constraint(text) == ConstraintType.decision

    def test_mixed_inline_markers_do_not_mislabel_the_whole_chunk(self):
        text = (
            "<!-- opensquilla-constraint: decision --> chose SQLite\n"
            "<!-- opensquilla-constraint: preference --> prefer local storage"
        )
        assert parse_inline_constraint(text) is None

    @pytest.mark.asyncio
    async def test_frontmatter_still_highest_priority(self):
        """Frontmatter override should bypass everything."""
        from opensquilla.memory.constraint_classifier import classify_constraint

        async def fake_llm(prompt: str) -> str:
            return "event"

        text = "---\nconstraint_type: goal\n---\nWe decided something important."
        ct, conf = await classify_constraint(text, llm_call=fake_llm)
        assert ct == ConstraintType.goal
        assert conf == 1.0


class TestA1SyncPath:
    """Sync path (classify_constraint_sync) should use heuristic-only (no escalation)."""

    def test_sync_high_confidence(self):
        ct, conf = classify_constraint_sync("我们最终决定采用 Redis 作为分布式缓存方案")
        assert ct == ConstraintType.decision
        assert conf == 0.6

    def test_sync_low_confidence(self):
        ct, conf = classify_constraint_sync("昨天下午我们部署了新版本的生产环境系统到线上服务器")
        assert ct == ConstraintType.event
        assert conf == 0.5

    def test_sync_inline_marker(self):
        text = "<!-- opensquilla-constraint: procedure --> install the package"
        ct, conf = classify_constraint_sync(text)
        assert ct == ConstraintType.procedure
        assert conf == 0.9


# ── A1-3: Store LLM Injection (index_file with async classify) ────────────


class TestA13StoreLlmInjection:
    """A1-3: store.index_file() uses async classify_constraint with LLM escalation."""

    @pytest.mark.asyncio
    async def test_llm_call_used_for_low_confidence(self, tmp_path):
        """Low-confidence heuristic text triggers LLM escalation via llm_call."""
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.types import MemorySource

        llm_calls: list[str] = []

        async def mock_llm(prompt: str) -> str:
            llm_calls.append(prompt)
            return "event"

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test_memory.db"),
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=True,
            constraint_llm_call=mock_llm,
        )
        await store.initialize()
        try:
            # "Yesterday we deployed..." has heuristic conf=0.5 < 0.6 → LLM escalated
            await store.index_file(
                "memory/test.md",
                "Yesterday we deployed the new version of the production"
                " environment system to the online server.",
                source=MemorySource.memory,
            )
            async with store._db.execute(
                "SELECT constraint_type, constraint_confidence FROM chunks WHERE path = ?",
                ("memory/test.md",),
            ) as cur:
                rows = await cur.fetchall()
            assert len(rows) == 1
            # LLM returned "event" with _LLM_CONFIDENCE=0.8
            assert rows[0][0] == "event"
            assert rows[0][1] == 0.8
            # LLM was actually called
            assert len(llm_calls) == 1
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_classification_cache_reuses_chunk_identity_across_files(self, tmp_path):
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.types import MemorySource

        llm_calls = 0

        async def mock_llm(_prompt: str) -> str:
            nonlocal llm_calls
            llm_calls += 1
            return "event"

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test_memory.db"),
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=True,
            constraint_llm_call=mock_llm,
        )
        await store.initialize()
        content = (
            "Yesterday we deployed the new version of the production "
            "environment system to the online server."
        )
        try:
            await store.index_file("memory/a.md", content, source=MemorySource.memory)
            await store.index_file("memory/b.md", content, source=MemorySource.memory)
            assert llm_calls == 1
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_llm_skipped_for_high_confidence(self, tmp_path):
        """High-confidence heuristic text (>= 0.6) skips LLM entirely."""
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.types import MemorySource

        llm_calls: list[str] = []

        async def mock_llm(prompt: str) -> str:
            llm_calls.append(prompt)
            return "fact"

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test_memory.db"),
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=True,
            constraint_llm_call=mock_llm,
        )
        await store.initialize()
        try:
            # "We decided to use PostgreSQL..." has heuristic conf=0.6 >= 0.6 → LLM skipped
            await store.index_file(
                "memory/test.md",
                "We decided to use PostgreSQL for the production database.",
                source=MemorySource.memory,
            )
            async with store._db.execute(
                "SELECT constraint_type, constraint_confidence FROM chunks WHERE path = ?",
                ("memory/test.md",),
            ) as cur:
                rows = await cur.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "decision"
            assert rows[0][1] == 0.6
            # LLM was NOT called (heuristic accepted directly)
            assert len(llm_calls) == 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_heuristic(self, tmp_path):
        """When LLM call raises, falls back to heuristic result."""
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.types import MemorySource

        async def failing_llm(prompt: str) -> str:
            raise RuntimeError("LLM unavailable")

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test_memory.db"),
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=True,
            constraint_llm_call=failing_llm,
        )
        await store.initialize()
        try:
            await store.index_file(
                "memory/test.md",
                "Yesterday we deployed the new version of the production"
                " environment system to the online server.",
                source=MemorySource.memory,
            )
            async with store._db.execute(
                "SELECT constraint_type, constraint_confidence FROM chunks WHERE path = ?",
                ("memory/test.md",),
            ) as cur:
                rows = await cur.fetchall()
            assert len(rows) == 1
            # Falls back to heuristic: event, 0.5
            assert rows[0][0] == "event"
            assert rows[0][1] == 0.5
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_no_llm_call_uses_sync_fallback(self, tmp_path):
        """Without llm_call, store uses classify_constraint_sync (current behavior)."""
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.types import MemorySource

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test_memory.db"),
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=True,
            constraint_llm_call=None,  # explicit None
        )
        await store.initialize()
        try:
            await store.index_file(
                "memory/test.md",
                "Yesterday we deployed the new version of the production"
                " environment system to the online server.",
                source=MemorySource.memory,
            )
            async with store._db.execute(
                "SELECT constraint_type, constraint_confidence FROM chunks WHERE path = ?",
                ("memory/test.md",),
            ) as cur:
                rows = await cur.fetchall()
            assert len(rows) == 1
            # Sync path: heuristic only, event 0.5
            assert rows[0][0] == "event"
            assert rows[0][1] == 0.5
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_annotation_off_ignores_llm_call(self, tmp_path):
        """With annotation disabled, llm_call is never invoked."""
        from opensquilla.memory.embedding import NullEmbeddingProvider
        from opensquilla.memory.store import LongTermMemoryStore
        from opensquilla.memory.types import MemorySource

        llm_calls: list[str] = []

        async def mock_llm(prompt: str) -> str:
            llm_calls.append(prompt)
            return "decision"

        store = LongTermMemoryStore(
            db_path=str(tmp_path / "test_memory.db"),
            embedding_provider=NullEmbeddingProvider(),
            constraint_annotation_enabled=False,
            constraint_llm_call=mock_llm,
        )
        await store.initialize()
        try:
            await store.index_file(
                "memory/test.md",
                "We decided to use PostgreSQL for the production database.",
                source=MemorySource.memory,
            )
            async with store._db.execute(
                "SELECT constraint_type, constraint_confidence FROM chunks WHERE path = ?",
                ("memory/test.md",),
            ) as cur:
                rows = await cur.fetchall()
            assert len(rows) == 1
            # Default: fact, NULL (not annotated)
            assert rows[0][0] == "fact"
            assert rows[0][1] is None
            assert len(llm_calls) == 0
        finally:
            await store.close()


class TestA13ManagerAdapter:
    """A1-3: _make_constraint_llm_call adapter tests."""

    @pytest.mark.asyncio
    async def test_adapter_with_complete_provider(self):
        """Provider with complete() → adapter returns text."""
        from opensquilla.memory.manager import _make_constraint_llm_call

        class FakeCompleteProvider:
            async def complete(self, messages, max_tokens=100):
                class Resp:
                    content = "decision"
                return Resp()

        llm_call = _make_constraint_llm_call(FakeCompleteProvider())
        assert llm_call is not None
        result = await llm_call("classify this text")
        assert result == "decision"

    @pytest.mark.asyncio
    async def test_adapter_records_usage_or_bounded_estimate(self):
        from opensquilla.memory.manager import _make_constraint_llm_call

        recorded: list[dict] = []

        class FakeCompleteProvider:
            model = "test-model"
            provider_id = "test-provider"

            async def complete(self, messages, max_tokens=100):
                class Resp:
                    content = "decision"
                    input_tokens = 12
                    output_tokens = 1

                return Resp()

        llm_call = _make_constraint_llm_call(
            FakeCompleteProvider(),
            usage_recorder=lambda **usage: recorded.append(usage),
        )
        assert llm_call is not None
        await llm_call("classify this text")

        assert recorded == [
            {
                "input_tokens": 12,
                "output_tokens": 1,
                "model_id": "test-model",
                "provider": "test-provider",
                "billed_cost": 0.0,
                "cost_source": "none",
            }
        ]

    @pytest.mark.asyncio
    async def test_adapter_with_chat_provider(self):
        """Provider with chat() (streaming) → adapter collects text_delta."""
        from opensquilla.memory.manager import _make_constraint_llm_call

        class FakeTextDelta:
            kind = "text_delta"
            text = "event"

        class FakeDone:
            kind = "done"
            text = ""

        class FakeChatProvider:
            def chat(self, messages, config=None):
                async def _stream():
                    yield FakeTextDelta()
                    yield FakeDone()
                return _stream()

        llm_call = _make_constraint_llm_call(FakeChatProvider())
        assert llm_call is not None
        result = await llm_call("classify this text")
        assert result == "event"

    def test_adapter_returns_none_for_no_provider(self):
        """None provider → None adapter."""
        from opensquilla.memory.manager import _make_constraint_llm_call

        assert _make_constraint_llm_call(None) is None

    def test_adapter_returns_none_for_unsupported_provider(self):
        """Provider without complete/chat → None adapter."""
        from opensquilla.memory.manager import _make_constraint_llm_call

        class EmptyProvider:
            pass

        assert _make_constraint_llm_call(EmptyProvider()) is None
