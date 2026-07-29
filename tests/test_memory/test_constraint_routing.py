"""Tests for L2: Constraint-aware retrieval routing.

Tests cover:
1. Query intent classification (heuristic, bilingual)
2. Boost computation (confidence threshold, clipping, neutral)
3. Boost application (re-sorting, score modification)
4. Provenance marker formatting (D9)
5. Degradation chain (L1 off, low confidence, general intent)
6. Regression: flag off = no-op
"""

from __future__ import annotations

import pytest

from opensquilla.memory.constraint_routing import (
    BOOST_MAX,
    BOOST_MIN,
    CONFIDENCE_THRESHOLD,
    QUERY_INTENT_BOOST,
    QueryIntent,
    apply_constraint_boost,
    classify_query_intent,
    compute_boost,
    format_provenance_marker,
    should_add_provenance_marker,
)
from opensquilla.memory.types import ConstraintType, MemorySearchResult, MemorySource


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_result(
    score: float = 0.8,
    ctype: str = "fact",
    confidence: float | None = 0.8,
    chunk_id: str = "test",
) -> MemorySearchResult:
    meta: dict[str, str] = {}
    if ctype:
        meta["constraint_type"] = ctype
    if confidence is not None:
        meta["constraint_confidence"] = str(confidence)
    return MemorySearchResult(
        chunk_id=chunk_id,
        path="memory/test.md",
        source=MemorySource.memory,
        start_line=1,
        end_line=10,
        snippet="test snippet",
        score=score,
        text=chunk_id,
        metadata=meta,
    )


# ── 1. Query Intent Classification ────────────────────────────────────────


class TestClassifyQueryIntent:
    def test_continue_task_english(self):
        intent, conf = classify_query_intent("continue the previous task")
        assert intent == QueryIntent.continue_task
        assert conf >= 0.7

    def test_continue_task_chinese(self):
        intent, conf = classify_query_intent("继续上次的工作")
        assert intent == QueryIntent.continue_task

    def test_retrieve_rationale_english(self):
        intent, conf = classify_query_intent("why was SQLite chosen?")
        assert intent == QueryIntent.retrieve_rationale

    def test_retrieve_rationale_chinese(self):
        intent, conf = classify_query_intent("为什么选择这个方案？")
        assert intent == QueryIntent.retrieve_rationale

    def test_avoid_failure_english(self):
        intent, conf = classify_query_intent("there is a problem with the deployment")
        assert intent == QueryIntent.avoid_failure

    def test_avoid_failure_chinese(self):
        intent, conf = classify_query_intent("部署失败了，报错信息")
        assert intent == QueryIntent.avoid_failure

    def test_transfer_knowledge_english(self):
        intent, conf = classify_query_intent("have we done something similar before?")
        assert intent == QueryIntent.transfer_knowledge

    def test_transfer_knowledge_chinese(self):
        intent, conf = classify_query_intent("有没有类似的经验可以借鉴？")
        assert intent == QueryIntent.transfer_knowledge

    def test_general_default(self):
        intent, conf = classify_query_intent("what is the capital of France?")
        assert intent == QueryIntent.general
        assert conf == 0.5

    def test_avoid_failure_priority_over_continue(self):
        """avoid_failure should win when both keywords present."""
        intent, _ = classify_query_intent("continue fixing the error")
        assert intent == QueryIntent.avoid_failure

    def test_empty_query(self):
        intent, conf = classify_query_intent("")
        assert intent == QueryIntent.general


# ── 2. Boost Computation ──────────────────────────────────────────────────


class TestComputeBoost:
    def test_goal_continue_task(self):
        r = _make_result(ctype="goal", confidence=0.8)
        boost = compute_boost(r, QueryIntent.continue_task)
        assert boost == 1.5

    def test_decision_retrieve_rationale(self):
        r = _make_result(ctype="decision", confidence=0.8)
        boost = compute_boost(r, QueryIntent.retrieve_rationale)
        assert boost == 1.5

    def test_decision_continue_task(self):
        r = _make_result(ctype="decision", confidence=0.8)
        boost = compute_boost(r, QueryIntent.continue_task)
        assert boost == 1.2

    def test_low_confidence_no_boost(self):
        r = _make_result(ctype="decision", confidence=0.5)
        boost = compute_boost(r, QueryIntent.retrieve_rationale)
        assert boost == 1.0

    def test_none_confidence_no_boost(self):
        r = _make_result(ctype="decision", confidence=None)
        boost = compute_boost(r, QueryIntent.retrieve_rationale)
        assert boost == 1.0

    def test_general_intent_no_boost(self):
        r = _make_result(ctype="goal", confidence=0.9)
        boost = compute_boost(r, QueryIntent.general)
        assert boost == 1.0

    def test_fact_type_no_boost_for_continue(self):
        r = _make_result(ctype="fact", confidence=0.9)
        boost = compute_boost(r, QueryIntent.continue_task)
        assert boost == 1.0

    def test_confidence_threshold_boundary(self):
        r = _make_result(ctype="goal", confidence=CONFIDENCE_THRESHOLD)
        boost = compute_boost(r, QueryIntent.continue_task)
        assert boost == 1.5  # exactly at threshold = boost applies

    def test_confidence_just_below_threshold(self):
        r = _make_result(ctype="goal", confidence=CONFIDENCE_THRESHOLD - 0.01)
        boost = compute_boost(r, QueryIntent.continue_task)
        assert boost == 1.0  # below threshold = neutral

    def test_clipping_max(self):
        assert BOOST_MAX == 1.8

    def test_clipping_min(self):
        assert BOOST_MIN == 0.85


# ── 3. Boost Application (re-sorting) ─────────────────────────────────────


class TestApplyConstraintBoost:
    def test_general_intent_no_change(self):
        results = [_make_result(score=0.9, ctype="fact"), _make_result(score=0.7, ctype="goal")]
        original_scores = [r.score for r in results]
        apply_constraint_boost(results, QueryIntent.general)
        assert [r.score for r in results] == original_scores

    def test_goal_boosted_above_fact(self):
        """With continue_task intent, goal should rank above fact even if
        initially lower."""
        results = [
            _make_result(score=0.9, ctype="fact", chunk_id="fact1"),
            _make_result(score=0.7, ctype="goal", confidence=0.8, chunk_id="goal1"),
        ]
        apply_constraint_boost(results, QueryIntent.continue_task)
        # goal boosted: 0.7 * 1.5 = 1.05 > fact: 0.9 * 1.0 = 0.9
        assert results[0].chunk_id == "goal1"
        assert results[1].chunk_id == "fact1"

    def test_low_confidence_not_boosted(self):
        """Low-confidence goal should not be boosted."""
        results = [
            _make_result(score=0.9, ctype="fact", chunk_id="fact1"),
            _make_result(score=0.7, ctype="goal", confidence=0.4, chunk_id="goal1"),
        ]
        apply_constraint_boost(results, QueryIntent.continue_task)
        assert results[0].chunk_id == "fact1"

    def test_none_confidence_not_boosted(self):
        results = [
            _make_result(score=0.9, ctype="decision", confidence=None, chunk_id="dec1"),
        ]
        apply_constraint_boost(results, QueryIntent.retrieve_rationale)
        assert results[0].score == 0.9  # unchanged

    def test_empty_results(self):
        results: list[MemorySearchResult] = []
        apply_constraint_boost(results, QueryIntent.continue_task)
        assert len(results) == 0

    def test_score_modified_in_place(self):
        r = _make_result(score=0.7, ctype="goal", confidence=0.8)
        results = [r]
        apply_constraint_boost(results, QueryIntent.continue_task)
        assert r.score == pytest.approx(0.7 * 1.5)

    def test_continue_task_prefers_goals_over_decisions(self):
        """goal 1.5x > decision 1.2x for continue_task."""
        decision = _make_result(score=0.75, ctype="decision", confidence=0.8, chunk_id="dec")
        goal = _make_result(score=0.70, ctype="goal", confidence=0.8, chunk_id="goal")
        procedure = _make_result(score=0.72, ctype="procedure", confidence=0.8, chunk_id="proc")
        results = apply_constraint_boost(
            [decision, goal, procedure],
            QueryIntent.continue_task,
        )
        # goal 1.5x -> 1.05, decision 1.2x -> 0.9, procedure 1.0x -> 0.72
        assert results[0].chunk_id == "goal"
        assert results[1].chunk_id == "dec"
        assert results[2].chunk_id == "proc"


# ── 4. Provenance Marker (D9) ─────────────────────────────────────────────


class TestProvenanceMarker:
    def test_should_add_marker_decision(self):
        r = _make_result(ctype="decision", confidence=0.8)
        assert should_add_provenance_marker(r) is True

    def test_should_not_add_marker_fact(self):
        r = _make_result(ctype="fact", confidence=0.8)
        assert should_add_provenance_marker(r) is False

    def test_should_not_add_marker_no_confidence(self):
        r = _make_result(ctype="decision", confidence=None)
        assert should_add_provenance_marker(r) is False

    def test_format_marker_decision(self):
        r = _make_result(ctype="decision", confidence=0.8)
        formatted = format_provenance_marker(r, "test content")
        assert '<memory_result type="decision" confidence="0.80">' in formatted
        assert "test content" in formatted
        assert "</memory_result>" in formatted

    def test_format_marker_procedure(self):
        r = _make_result(ctype="procedure", confidence=0.6)
        formatted = format_provenance_marker(r, "step 1\nstep 2")
        assert '<memory_result type="procedure" confidence="0.60">' in formatted
        assert "step 1" in formatted

    def test_format_marker_unknown_confidence(self):
        """If confidence is None, marker uses 'unknown'."""
        r = _make_result(ctype="decision", confidence=None)
        formatted = format_provenance_marker(r, "content")
        assert 'confidence="unknown"' in formatted


# ── 5. Degradation Chain ──────────────────────────────────────────────────


class TestDegradationChain:
    def test_l1_off_all_fact_no_change(self):
        """When L1 is off, all chunks are 'fact' -> no boost."""
        results = [
            _make_result(score=0.9, ctype="fact", confidence=None, chunk_id="a"),
            _make_result(score=0.7, ctype="fact", confidence=None, chunk_id="b"),
        ]
        original_order = [r.chunk_id for r in results]
        apply_constraint_boost(results, QueryIntent.continue_task)
        assert [r.chunk_id for r in results] == original_order

    def test_intent_general_no_op(self):
        results = [
            _make_result(score=0.9, ctype="goal", confidence=0.9, chunk_id="a"),
        ]
        original_score = results[0].score
        apply_constraint_boost(results, QueryIntent.general)
        assert results[0].score == original_score

    def test_confidence_below_threshold_no_op(self):
        results = [
            _make_result(score=0.9, ctype="goal", confidence=0.3, chunk_id="a"),
            _make_result(score=0.8, ctype="decision", confidence=0.2, chunk_id="b"),
        ]
        original_order = [r.chunk_id for r in results]
        apply_constraint_boost(results, QueryIntent.continue_task)
        assert [r.chunk_id for r in results] == original_order

    def test_mixed_confidence(self):
        """Only high-confidence results get boosted."""
        results = [
            _make_result(score=0.8, ctype="goal", confidence=0.8, chunk_id="high"),
            _make_result(score=0.85, ctype="goal", confidence=0.4, chunk_id="low"),
        ]
        apply_constraint_boost(results, QueryIntent.continue_task)
        # high: 0.8 * 1.5 = 1.2, low: 0.85 * 1.0 = 0.85
        assert results[0].chunk_id == "high"
        assert results[1].chunk_id == "low"


# ── 6. Regression: Flag Off = No-op ───────────────────────────────────────


class TestRegressionNoOp:
    def test_boost_values_bounded(self):
        """All boost values in the map must be within [0.85, 1.8]."""
        for intent, boost_map in QUERY_INTENT_BOOST.items():
            for ctype, boost_val in boost_map.items():
                assert BOOST_MIN <= boost_val <= BOOST_MAX, (
                    f"Boost {boost_val} for {intent}/{ctype} out of bounds"
                )

    def test_no_boost_exceeds_max(self):
        r = _make_result(score=1.0, ctype="goal", confidence=1.0)
        boost = compute_boost(r, QueryIntent.continue_task)
        assert boost <= BOOST_MAX

    def test_default_fact_has_no_boost(self):
        """fact type should never get a boost in any intent."""
        for intent, boost_map in QUERY_INTENT_BOOST.items():
            assert ConstraintType.fact not in boost_map, (
                f"fact should not have boost for {intent}"
            )

    def test_avoid_failure_v07_empty_core_types(self):
        """v0.7: avoid_failure boost map should not contain core types."""
        af_map = QUERY_INTENT_BOOST[QueryIntent.avoid_failure]
        core = {
            ConstraintType.fact,
            ConstraintType.event,
            ConstraintType.preference,
            ConstraintType.decision,
            ConstraintType.procedure,
            ConstraintType.goal,
        }
        for ct in af_map:
            assert ct not in core, f"v0.7 avoid_failure should not boost core type {ct}"


# ── B6: Chinese Intent Expansion ────────────────────────────────────────────


class TestB6ChineseExpandedKeywords:
    """B6: expanded Chinese keyword patterns for all 4 intents."""

    def test_avoid_failure_chinese_expanded(self):
        for q in [
            "系统挂了",
            "服务宕机了",
            "请求超时了",
            "死锁了",
            "回滚失败",
            "紧急修复",
            "严重bug",
            "阻塞了",
            "不工作了",
            "跑不通",
            "出错了",
        ]:
            intent, conf = classify_query_intent(q)
            assert intent == QueryIntent.avoid_failure, f"Expected avoid_failure for: {q}, got {intent}"
            assert conf == 0.7

    def test_continue_task_chinese_expanded(self):
        for q in [
            "回到之前的话题",
            "进展如何了",
            "做到哪儿了",
            "停在哪里了",
            "还没做完",
            "待完成的任务",
            "后续步骤",
            "往下做",
        ]:
            intent, conf = classify_query_intent(q)
            assert intent == QueryIntent.continue_task, f"Expected continue_task for: {q}, got {intent}"
            assert conf == 0.7

    def test_retrieve_rationale_chinese_expanded(self):
        for q in [
            "为啥这么做",
            "凭什么",
            "根据什么",
            "出于什么原因",
            "动机是什么",
            "根因是什么",
            "根本原因",
        ]:
            intent, conf = classify_query_intent(q)
            assert intent == QueryIntent.retrieve_rationale, f"Expected retrieve_rationale for: {q}, got {intent}"
            assert conf == 0.7

    def test_transfer_knowledge_chinese_expanded(self):
        for q in [
            "参考一下",
            "有没有先例",
            "借鉴一下",
            "类似情况",
            "相似的经验",
        ]:
            intent, conf = classify_query_intent(q)
            assert intent == QueryIntent.transfer_knowledge, f"Expected transfer_knowledge for: {q}, got {intent}"
            assert conf == 0.6

    def test_avoid_failure_english_expanded(self):
        for q in [
            "got an exception in the parser",
            "the request timeout after 30s",
            "there is a deadlock in the database",
            "fatal error in production",
            "the test is flaky",
        ]:
            intent, conf = classify_query_intent(q)
            assert intent == QueryIntent.avoid_failure, f"Expected avoid_failure for: {q}, got {intent}"

    def test_continue_task_english_expanded(self):
        for q in [
            "proceed with the deployment",
            "what's next on the agenda",
            "carry on with the implementation",
        ]:
            intent, conf = classify_query_intent(q)
            assert intent == QueryIntent.continue_task, f"Expected continue_task for: {q}, got {intent}"

    def test_transfer_knowledge_english_expanded(self):
        for q in [
            "is there a precedent for this approach",
            "any prior art on this topic",
            "looking for a comparable solution",
        ]:
            intent, conf = classify_query_intent(q)
            assert intent == QueryIntent.transfer_knowledge, f"Expected transfer_knowledge for: {q}, got {intent}"


# ── B6: Negation Detection ─────────────────────────────────────────────────


class TestB6NegationDetection:
    """B6: negation words should prevent keyword matching."""

    def test_negation_blocks_avoid_failure(self):
        for q in [
            "没有问题",
            "没有错误",
            "不是bug",
            "不存在故障",
            "无需修复",
            "不要报错",
            "没失败",
            "未崩溃",
            "无异常",
        ]:
            intent, conf = classify_query_intent(q)
            assert intent == QueryIntent.general, f"Expected general (negated) for: {q}, got {intent}"

    def test_negation_blocks_continue_task(self):
        for q in [
            "没有继续做",
            "不要接着",
            "别继续了",
        ]:
            intent, conf = classify_query_intent(q)
            assert intent == QueryIntent.general, f"Expected general (negated) for: {q}, got {intent}"

    def test_negation_blocks_transfer_knowledge(self):
        intent, conf = classify_query_intent("没有类似经验")
        assert intent == QueryIntent.general

    def test_negation_only_blocks_when_adjacent(self):
        """Negation must be immediately before the keyword, not far away."""
        # "崩溃" is far from "没有" (separated by "关系，但是服务")
        intent, conf = classify_query_intent("没有关系，但是服务崩溃了")
        assert intent == QueryIntent.avoid_failure

    def test_positive_still_works_after_negation_feature(self):
        """Non-negated queries should still match correctly."""
        intent, conf = classify_query_intent("这里有个问题需要解决")
        assert intent == QueryIntent.avoid_failure


# ── B6: Interrogative Exclusion ─────────────────────────────────────────────


class TestB6InterrogativeExclusion:
    """B6: interrogative markers should NOT be treated as negations."""

    def test_youmeiyou_does_not_block_avoid_failure(self):
        # "有没有问题" = "is there a problem" → should match
        intent, conf = classify_query_intent("有没有问题")
        assert intent == QueryIntent.avoid_failure

    def test_youmeiyou_does_not_block_transfer(self):
        # "有没有类似的经验可以借鉴？" → should match transfer_knowledge
        intent, conf = classify_query_intent("有没有类似的经验可以借鉴？")
        assert intent == QueryIntent.transfer_knowledge

    def test_shibushi_does_not_block(self):
        intent, conf = classify_query_intent("是不是可以继续了")
        assert intent == QueryIntent.continue_task

    def test_huibuhui_does_not_block(self):
        intent, conf = classify_query_intent("会不会失败")
        assert intent == QueryIntent.avoid_failure

    def test_nengbuneng_does_not_block(self):
        intent, conf = classify_query_intent("能不能解释一下原因")
        assert intent == QueryIntent.retrieve_rationale

    def test_keyibukei_does_not_block(self):
        intent, conf = classify_query_intent("可不可以解释")
        assert intent == QueryIntent.retrieve_rationale


# ── B6: English Regression ─────────────────────────────────────────────────


class TestB6EnglishRegression:
    """B6 changes should not affect English keyword matching."""

    def test_english_avoid_failure(self):
        for q in ["there is a problem", "got an error", "it crashed"]:
            intent, conf = classify_query_intent(q)
            assert intent == QueryIntent.avoid_failure

    def test_english_continue_task(self):
        intent, conf = classify_query_intent("continue from where we left off")
        assert intent == QueryIntent.continue_task

    def test_english_negation_known_limitation(self):
        """English negation is not handled (v0.7 scope, documented in B6 spec)."""
        # "no problem" still matches avoid_failure — known limitation
        intent, conf = classify_query_intent("no problem, everything is fine")
        assert intent == QueryIntent.avoid_failure  # documented limitation
