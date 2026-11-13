from __future__ import annotations

from dataclasses import replace

import pytest

from opensquilla.engine.jev_context import (
    build_context_restart_assessment,
    maybe_publish_context_restart,
)
from opensquilla.engine.pipeline import TurnContext
from opensquilla.gateway.config import GatewayConfig
from opensquilla.jev_context import (
    ContextRestartAssessment,
    ContextRestartDecision,
    context_restart_question,
    context_restart_state,
    parse_context_restart_answer,
)
from opensquilla.jev_service import JevService

SESSION = "agent:main:webchat:synthetic-context-assessment"


def make_turn(*, history=1800, completed=20, **metadata):
    config = GatewayConfig(jev_router={"enabled": True, "context_restart_min_tokens": 1024})
    return TurnContext(
        message="Continue the synthetic analysis.", session_key=SESSION,
        config=config, provider=None, model="sample-model", tool_defs=[], system_prompt="",
        metadata={
            "routing_history_capacity_estimated_tokens": history,
            "routing_history_capacity_estimate_complete": True,
            "jev_completed_conversation_turns": completed,
            "jev_turn_count": completed + 1,
            **metadata,
        },
    )


def assess(turn, window=2000):
    return build_context_restart_assessment(
        turn, config=turn.config.jev_router, context_window_tokens=window,
    )


def native_answer(choice="new_session", score=0.9):
    other = "keep_context" if choice == "new_session" else "new_session"
    return {"type": "choice", "choice": choice,
            "probabilities": {choice: score, other: 1 - score}}


async def publish(turn, service, *, decision=None, assessment=None, window=2000, **options):
    return await maybe_publish_context_restart(
        turn, service=service, config=turn.config.jev_router, input_id="synthetic-draft",
        current_model=turn.model, current_provider="sample-provider", context_window_tokens=window,
        assessment_stage="loop", assessment=assessment, decision=decision, **options,
    )


@pytest.mark.parametrize("history,window,eligible", [
    (0, 4096, False), (100_000, 0, False),
    (799, 1000, False), (800, 1000, False),
    (1023, 1000, False), (1024, 1000, True),
    (1599, 2000, False), (1600, 2000, True),
    (1639, 2049, False), (1640, 2049, True),
])
def test_completed_turns_and_maximum_pressure_threshold_are_both_required(
    history, window, eligible,
):
    turn = make_turn(history=history)
    assessment = assess(turn, window)
    assert (assessment is not None) is eligible
    if assessment is not None:
        assert assessment.completed_turns == 20
        assert assessment.history_tokens == history
        assert assessment.context_window_tokens == window
        assert context_restart_state(assessment)["history_pressure"] is (
            history >= assessment.threshold_tokens
        )


@pytest.mark.parametrize("history,window,completed,eligible", [
    (10_000, 1_000_000, 20, False),
    (32_000, 1_000_000, 20, False),
    (799_999, 1_000_000, 20, False),
    (800_000, 1_000_000, 20, True),
    (90_000, 100_000, 19, False),
    (80_000, 100_000, 20, True),
    (31_999, 32_000, 20, False),
    (32_000, 32_000, 20, True),
])
def test_default_thresholds_do_not_confuse_many_turns_or_absolute_tokens_with_pressure(
    history, window, completed, eligible,
):
    turn = make_turn(history=history, completed=completed)
    turn.config.jev_router.context_restart_min_tokens = 32_000
    assert turn.config.jev_router.context_restart_min_turns == 20
    assert turn.config.jev_router.context_restart_ratio == 0.8
    assert (assess(turn, window) is not None) is eligible


@pytest.mark.parametrize("window", [0, -1, True, 1.5, float("nan"), float("inf"), None, "2000"])
def test_unknown_or_invalid_current_window_fails_closed(window):
    assert assess(make_turn(history=1_000_000), window) is None


@pytest.mark.parametrize("completed", [0, 1, 2, 19])
def test_few_conversation_rounds_never_ask_for_restart_even_with_huge_tool_history(completed):
    turn = make_turn(history=1_000_000, completed=completed, loop_iteration=500)
    assert assess(turn) is None


def test_large_fresh_input_does_not_count_as_accumulated_history():
    turn = make_turn(history=0, completed=200)
    turn.message = "A large new source document. " * 50_000
    assert assess(turn) is None


def test_authoritative_completed_count_excludes_active_input_and_overrides_legacy_count():
    turn = make_turn(completed=0, jev_turn_count=80)
    assert assess(turn) is None
    turn.metadata.pop("jev_completed_conversation_turns")
    turn.metadata["jev_turn_count"] = 20
    assert assess(turn) is None
    turn.metadata["jev_turn_count"] = 21
    assert assess(turn).completed_turns == 20


@pytest.mark.parametrize("value", [True, -1, 1.5, float("nan"), float("inf"), 10**400])
def test_invalid_completed_count_cannot_make_assessment_eligible(value):
    turn = make_turn()
    turn.metadata["jev_completed_conversation_turns"] = value
    assert assess(turn) is None


@pytest.mark.parametrize("value", [True, -1, 1.5, float("nan"), float("inf"), 10**400])
def test_invalid_history_measurement_cannot_make_assessment_eligible(value):
    assert assess(make_turn(history=value)) is None


def test_incomplete_history_measurement_and_disabled_budget_defer_assessment():
    turn = make_turn(routing_history_capacity_estimate_complete=False)
    assert assess(turn) is None
    turn.metadata["routing_history_capacity_estimate_complete"] = True
    turn.config.jev_router.max_prompts_per_hour = 0
    assert assess(turn) is None
    turn.config.jev_router.max_prompts_per_hour = 3
    turn.config.jev_router.session_restart_enabled = False
    assert assess(turn) is None


def test_native_question_carries_history_facts_separate_from_model_selection():
    assessment = assess(make_turn())
    question = context_restart_question(assessment)
    assert question["type"] == "choice"
    assert set(question["criteria"]) == {"keep_context", "new_session"}
    assert context_restart_state(assessment) == {
        "completed_conversation_turns": 20,
        "history_tokens": 1800,
        "context_window_tokens": 2000,
        "history_threshold_tokens": 1600,
        "history_pressure": True,
    }


@pytest.mark.parametrize("choice,score,action", [
    ("keep_context", 0.95, "keep_context"),
    ("new_session", 0.6, "keep_context"),
    ("new_session", 0.9, "new_session"),
])
def test_native_choice_and_selected_probability_control_the_decision(choice, score, action):
    result = parse_context_restart_answer(
        native_answer(choice, score), assess(make_turn()), threshold=0.65,
    )
    assert result.action == action
    assert result.confidence == score


@pytest.mark.parametrize("answer", [
    None, {},
    {"type": "choice", "choice": "new_session", "probabilities": {"new_session": 1}},
    {"type": "choice", "choice": "new_session",
     "probabilities": {"new_session": 0.2, "keep_context": 0.8}},
    {"type": "choice", "choice": "new_session",
     "probabilities": {"new_session": 0.9, "keep_context": 0.9}},
    {"type": "choice", "choice": "new_session",
     "probabilities": {"new_session": True, "keep_context": 0}},
    {"type": "choice", "choice": "new_session",
     "probabilities": {"new_session": "nan", "keep_context": 0}},
    {"type": "choice", "choice": "new_session", "confidence": "not-a-number",
     "probabilities": {"new_session": 0.9, "keep_context": 0.1}},
])
def test_malformed_optional_native_answer_cannot_authorize_a_notice(answer):
    with pytest.raises(ValueError):
        parse_context_restart_answer(answer, assess(make_turn()), threshold=0.65)


async def test_history_and_round_thresholds_never_publish_without_positive_native_decision():
    turn = make_turn()
    service = JevService()
    try:
        assert not await publish(turn, service)
        assert not await publish(
            turn, service, decision=ContextRestartDecision("keep_context", 0.95),
        )
        assert not await publish(
            turn, service, decision=ContextRestartDecision("new_session", 0.5),
        )
        assert not (await service.snapshot(SESSION, turn.config.jev_router))["decisions"]
    finally:
        await service.close()


async def test_native_positive_notice_is_manual_even_in_trust_and_does_not_block_agent():
    turn = make_turn()
    turn.message = "继续检查这份合成报告。"
    turn.config.jev_router.mode = "trust"
    assessment = assess(turn)
    decision = parse_context_restart_answer(native_answer(), assessment, threshold=0.65)
    service = JevService()
    try:
        assert await publish(turn, service, decision=decision, assessment=assessment)
        snapshot = await service.snapshot(SESSION, turn.config.jev_router)
        notice = snapshot["recommendation"]
        assert notice["action"] == "new_session" and notice["target"] is None
        assert notice["source"] == "loop"
        assert notice["handoff"]["completedTurns"] == 20
        assert "JEV" in notice["reason"] and "20" in notice["evidence"][0]
        assert "autoSwitchAt" not in notice
        assert not snapshot["blocked"] and "jev_decision_id" not in turn.metadata
        await service.settle_deadline(SESSION, notice["id"], now_ms=10**15)
        assert (await service.get_decision(SESSION, notice["id"]))["status"] == "pending"
    finally:
        await service.close()


async def test_stale_generation_or_changed_history_cannot_publish_native_answer():
    turn = make_turn()
    assessment = assess(turn)
    decision = parse_context_restart_answer(native_answer(), assessment, threshold=0.65)
    service = JevService()
    service._generations[SESSION] = 2
    try:
        assert not await publish(
            turn, service, decision=decision, assessment=assessment, check_generation=1,
        )
        turn.metadata["routing_history_capacity_estimated_tokens"] += 100
        assert not await publish(turn, service, decision=decision, assessment=assessment)
        assert not (await service.snapshot(SESSION, turn.config.jev_router))["decisions"]
    finally:
        await service.close()


async def test_declined_native_notice_survives_draft_edits_and_allows_grown_history():
    turn = make_turn()
    service = JevService()
    decision = ContextRestartDecision("new_session", 0.9)
    try:
        assert await publish(turn, service, decision=decision)
        notice = (await service.snapshot(SESSION, turn.config.jev_router))["recommendation"]
        await service.respond(SESSION, notice["id"], False)
        turn.message = "An edited draft of the next synthetic question."
        assert not await publish(turn, service, decision=decision)
        turn.metadata["routing_history_capacity_estimated_tokens"] = 3200
        assert await publish(turn, service, decision=decision)
    finally:
        await service.close()


def test_ineligible_assessment_cannot_be_repaired_by_a_positive_native_choice():
    eligible = assess(make_turn())
    with pytest.raises(ValueError):
        parse_context_restart_answer(
            native_answer(), replace(eligible, history_tokens=0), threshold=0.65,
        )
    with pytest.raises(ValueError):
        parse_context_restart_answer(
            native_answer(), ContextRestartAssessment(1, 1800, 2000, 1600), threshold=0.65,
        )


@pytest.mark.parametrize("history,window,completed", [
    (10_000, 1_000_000, 20),
    (32_000, 1_000_000, 20),
    (90_000, 100_000, 19),
    (100_000, 0, 20),
])
async def test_forged_positive_cannot_publish_when_current_eligibility_is_missing(
    history, window, completed,
):
    turn = make_turn(history=history, completed=completed)
    turn.config.jev_router.context_restart_min_tokens = 32_000
    forged_assessment = ContextRestartAssessment(
        completed_turns=20, history_tokens=800_000,
        context_window_tokens=1_000_000, threshold_tokens=800_000,
    )
    service = JevService()
    try:
        assert not await publish(
            turn, service, window=window, assessment=forged_assessment,
            decision=ContextRestartDecision("new_session", 0.999),
        )
        assert not (await service.snapshot(SESSION, turn.config.jev_router))["decisions"]
    finally:
        await service.close()


@pytest.mark.parametrize("assessment", [
    ContextRestartAssessment(20, 799_999, 1_000_000, 800_000),
    ContextRestartAssessment(20, 100_000, 0, 32_000),
])
def test_native_positive_cannot_bypass_pressure_or_known_window(assessment):
    with pytest.raises(ValueError):
        parse_context_restart_answer(native_answer(), assessment, threshold=0.65)


@pytest.mark.parametrize("status", ["pending", "accepted"])
async def test_restart_expires_legacy_low_pressure_context_notice(tmp_path, status):
    path = tmp_path / "jev.db"
    turn = make_turn()
    service = JevService(path)
    notice = await service.publish(SESSION, {
        "action": "new_session", "current": {"provider": "sample", "model": turn.model},
        "target": None, "reason": "A legacy low-pressure reminder.", "risk": "medium",
        "handoff": {"sourceSessionKey": SESSION, "completedTurns": 20,
                    "contextTokens": 10_000, "contextWindowTokens": 1_000_000},
    }, input_id="legacy-low-pressure")
    if status == "accepted":
        await service.respond(SESSION, notice["id"], True)
    await service.close()
    reopened = JevService(path)
    try:
        snapshot = await reopened.snapshot(SESSION, turn.config.jev_router)
        assert snapshot["recommendation"] is None
        assert (await reopened.get_decision(SESSION, notice["id"]))["status"] == "expired"
    finally:
        await reopened.close()
