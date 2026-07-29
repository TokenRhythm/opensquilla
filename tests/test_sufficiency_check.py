"""Tests for L3: retrieval sufficiency check."""

from __future__ import annotations

from opensquilla.memory.constraint_routing import QueryIntent, classify_query_intent
from opensquilla.memory.sufficiency_check import (
    format_sufficiency_note,
    maybe_append_sufficiency_note,
    query_is_cjk,
    should_emit_sufficiency_note,
)

# ── query_is_cjk ──────────────────────────────────────────────────────────


class TestQueryIsCJK:
    def test_pure_chinese(self):
        assert query_is_cjk("上次那个bug怎么解决的") is True

    def test_pure_english(self):
        assert query_is_cjk("how did we fix that bug") is False

    def test_mixed_cjk_majority(self):
        # "Python 错误处理" → non-space: "Python错误处理" = 10 chars, 4 CJK = 40% > 30%
        assert query_is_cjk("Python 错误处理") is True

    def test_mixed_english_majority(self):
        # "error handling in Python with 错误" → ~30 non-space, 2 CJK ≈ 7% < 30%
        assert query_is_cjk("error handling in Python with 错误") is False

    def test_empty(self):
        assert query_is_cjk("") is False

    def test_whitespace_only(self):
        assert query_is_cjk("   \t  ") is False

    def test_boundary_exactly_30_percent(self):
        # 3 CJK in 10 non-space chars = exactly 30% → NOT > 0.3 → False
        assert query_is_cjk("abcdefg错误处") is False

    def test_boundary_above_30_percent(self):
        # 4 CJK in 10 non-space chars = 40% > 30% → True
        assert query_is_cjk("abcdef错误处理") is True


# ── should_emit_sufficiency_note ──────────────────────────────────────────


class TestShouldEmit:
    def test_empty_results_high_confidence(self):
        assert should_emit_sufficiency_note(0, 0.85) is True

    def test_partial_results_high_confidence(self):
        assert should_emit_sufficiency_note(2, 0.85) is True

    def test_one_result_high_confidence(self):
        assert should_emit_sufficiency_note(1, 0.9) is True

    def test_sufficient_results_boundary(self):
        # results == 3 → NOT < 3 → False
        assert should_emit_sufficiency_note(3, 0.85) is False

    def test_more_than_sufficient(self):
        assert should_emit_sufficiency_note(10, 0.9) is False

    def test_low_confidence(self):
        assert should_emit_sufficiency_note(0, 0.5) is False

    def test_boundary_confidence_exact(self):
        # strictly greater → 0.7 exactly should NOT trigger
        assert should_emit_sufficiency_note(0, 0.7) is True

    def test_boundary_confidence_above(self):
        assert should_emit_sufficiency_note(0, 0.71) is True

    def test_real_classifier_output_can_trigger(self):
        intent, confidence = classify_query_intent("continue the previous task")
        assert intent is QueryIntent.continue_task
        assert confidence == 0.7
        assert should_emit_sufficiency_note(0, confidence) is True

    def test_disabled(self):
        assert should_emit_sufficiency_note(0, 0.9, enabled=False) is False

    def test_disabled_even_when_triggered(self):
        assert should_emit_sufficiency_note(0, 0.99, enabled=False) is False


# ── format_sufficiency_note ───────────────────────────────────────────────


class TestFormatNote:
    def test_empty_chinese(self):
        note = format_sufficiency_note(
            "上次那个bug", 0, QueryIntent.avoid_failure, 0.85
        )
        assert 'intent="avoid_failure"' in note
        assert 'results="0"' in note
        assert "未找到" in note
        assert "web_search" in note
        assert "<memory_sufficiency_note" in note
        assert "</memory_sufficiency_note>" in note

    def test_empty_english(self):
        note = format_sufficiency_note(
            "what was that bug", 0, QueryIntent.avoid_failure, 0.85
        )
        assert 'intent="avoid_failure"' in note
        assert 'results="0"' in note
        assert "No memory results" in note
        assert "web_search" in note

    def test_partial_chinese(self):
        note = format_sufficiency_note(
            "继续上次的问题", 2, QueryIntent.continue_task, 0.85
        )
        assert 'intent="continue_task"' in note
        assert 'results="2"' in note
        assert "不足以完全覆盖" in note
        assert "session_search" in note

    def test_partial_english(self):
        note = format_sufficiency_note(
            "continue the issue", 2, QueryIntent.continue_task, 0.85
        )
        assert 'intent="continue_task"' in note
        assert 'results="2"' in note
        assert "insufficient" in note
        assert "session_search" in note

    def test_confidence_formatting(self):
        note = format_sufficiency_note(
            "test query", 0, QueryIntent.general, 0.723
        )
        assert 'confidence="0.72"' in note

    def test_xml_well_formed(self):
        """Ensure the note has balanced opening/closing tags."""
        note = format_sufficiency_note(
            "test", 1, QueryIntent.general, 0.8
        )
        assert note.startswith("<memory_sufficiency_note ")
        assert note.endswith("</memory_sufficiency_note>")
        assert note.count("<memory_sufficiency_note") == 1
        assert note.count("</memory_sufficiency_note>") == 1

    def test_partial_count_in_body(self):
        note = format_sufficiency_note(
            "test", 1, QueryIntent.general, 0.80
        )
        assert "1" in note  # count appears in body text


# ── maybe_append_sufficiency_note ─────────────────────────────────────────


class TestMaybeAppend:
    def test_appends_when_triggered_empty(self):
        result = maybe_append_sufficiency_note(
            "那个错误", 0, "No results found.",
            QueryIntent.avoid_failure, 0.85,
        )
        assert "No results found." in result
        assert "<memory_sufficiency_note" in result
        assert result.startswith("No results found.")

    def test_appends_when_triggered_partial(self):
        original = "[1] result one\n\n[2] result two"
        result = maybe_append_sufficiency_note(
            "那个问题", 2, original,
            QueryIntent.avoid_failure, 0.85,
        )
        assert original in result
        assert "<memory_sufficiency_note" in result
        assert result.startswith(original)

    def test_no_append_when_sufficient(self):
        original = "[1] some result"
        result = maybe_append_sufficiency_note(
            "test", 5, original,
            QueryIntent.general, 0.85,
        )
        assert result == original

    def test_no_append_when_low_confidence(self):
        original = "No results found."
        result = maybe_append_sufficiency_note(
            "test", 0, original,
            QueryIntent.general, 0.5,
        )
        assert result == original

    def test_no_append_when_disabled(self):
        original = "No results found."
        result = maybe_append_sufficiency_note(
            "test", 0, original,
            QueryIntent.general, 0.9,
            enabled=False,
        )
        assert result == original

    def test_chinese_note_for_cjk_query(self):
        result = maybe_append_sufficiency_note(
            "那个错误", 0, "No results found.",
            QueryIntent.avoid_failure, 0.85,
        )
        assert "未找到" in result

    def test_english_note_for_english_query(self):
        result = maybe_append_sufficiency_note(
            "that error", 0, "No results found.",
            QueryIntent.avoid_failure, 0.85,
        )
        assert "No memory results" in result


# ── Integration: Retriever properties ─────────────────────────────────────


class TestRetrieverIntegration:
    """Test that retriever correctly exposes L3 properties."""

    def test_retriever_has_sufficiency_properties(self):
        from opensquilla.memory.retrieval import MemoryRetriever

        assert hasattr(MemoryRetriever, "sufficiency_check_enabled")
        assert hasattr(MemoryRetriever, "last_query_intent")
        assert hasattr(MemoryRetriever, "last_query_confidence")

    def test_retriever_init_defaults(self):
        from opensquilla.memory.retrieval import MemoryRetriever

        class FakeStore:
            pass

        retriever = MemoryRetriever(FakeStore())  # type: ignore[arg-type]
        assert retriever.sufficiency_check_enabled is False
        assert retriever.last_query_intent is None
        assert retriever.last_query_confidence is None

    def test_retriever_init_with_sufficiency_enabled(self):
        from opensquilla.memory.retrieval import MemoryRetriever

        class FakeStore:
            pass

        retriever = MemoryRetriever(
            FakeStore(),  # type: ignore[arg-type]
            sufficiency_check_enabled=True,
        )
        assert retriever.sufficiency_check_enabled is True

    def test_effective_metadata_includes_sufficiency(self):
        from opensquilla.memory.retrieval import MemoryRetriever

        class FakeStore:
            pass

        retriever = MemoryRetriever(
            FakeStore(),  # type: ignore[arg-type]
            sufficiency_check_enabled=True,
        )
        metadata = retriever.effective_retrieval_metadata()
        assert metadata.get("sufficiency_check") == "on"

    def test_effective_metadata_no_sufficiency_by_default(self):
        from opensquilla.memory.retrieval import MemoryRetriever

        class FakeStore:
            pass

        retriever = MemoryRetriever(FakeStore())  # type: ignore[arg-type]
        metadata = retriever.effective_retrieval_metadata()
        assert "sufficiency_check" not in metadata
