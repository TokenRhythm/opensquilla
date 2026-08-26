from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from opensquilla.engine.routing.fixed_four_tier_v2 import (
    FEATURE_SCHEMA_VERSION,
    FEATURE_VECTOR_DIM,
    FEATURE_VECTOR_STATUS,
    INTENTS,
    TIERS,
    ClassifierPrediction,
    FixedFourTierDecision,
    FixedFourTierTaskState,
    FixedFourTierV2Router,
    RoutingRequest,
    Tier,
    normalize_attachment_modalities,
)


def _prediction(
    label: str,
    *,
    confidence: float | None = 1.0,
    probabilities: Mapping[str, float] | None = None,
) -> ClassifierPrediction:
    if probabilities is None:
        labels = INTENTS if label in INTENTS else TIERS
        probabilities = {item: 1.0 if item == label else 0.0 for item in labels}
    return ClassifierPrediction(
        label=label,
        probabilities=probabilities,
        confidence=confidence,
        version="scripted-v1",
    )


@dataclass
class _ScriptedIntentClassifier:
    results: list[ClassifierPrediction | Exception]
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    version: str = "scripted-intent-v1"

    def predict(self, snapshot: Mapping[str, Any]) -> ClassifierPrediction:
        self.snapshots.append(dict(snapshot))
        if not self.results:
            raise AssertionError("intent classifier called more than expected")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class _ScriptedTierClassifier:
    results: list[ClassifierPrediction | Exception]
    calls: list[tuple[dict[str, Any], tuple[Tier, ...]]] = field(default_factory=list)
    version: str = "scripted-tier-v1"

    def predict(
        self,
        snapshot: Mapping[str, Any],
        allowed_tiers: Sequence[Tier],
    ) -> ClassifierPrediction:
        self.calls.append((dict(snapshot), tuple(allowed_tiers)))
        if not self.results:
            raise AssertionError("tier classifier called more than expected")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _request(
    request_id: str,
    message: str = "请处理这个请求",
    *,
    session_id: str = "session-a",
    control_event: str | None = None,
    user_history: tuple[str, ...] = (),
    previous_assistant_text: str | None = None,
    previous_assistant_usage: Mapping[str, Any] | None = None,
    input_message_id: str | None = None,
    attachment_count: int = 0,
    attachment_modalities: tuple[str, ...] | None = None,
) -> RoutingRequest:
    return RoutingRequest(
        session_id=session_id,
        request_id=request_id,
        message=message,
        input_message_id=input_message_id,
        control_event=control_event,
        user_history=user_history,
        previous_assistant_text=previous_assistant_text,
        previous_assistant_usage=previous_assistant_usage,
        attachment_count=attachment_count,
        attachment_modalities=attachment_modalities,
    )


def _id_factory(prefix: str) -> Any:
    counter = 0

    def _next() -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}-{counter}"

    return _next


def test_first_request_is_new_task_and_skips_intent_classifier() -> None:
    intent = _ScriptedIntentClassifier([])
    tier = _ScriptedTierClassifier([_prediction("c2")])
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
        route_id_factory=_id_factory("route"),
        task_id_factory=_id_factory("task"),
    )

    decision, next_state = router.decide(
        _request(
            "request-1",
            user_history=("old unrelated request",),
            previous_assistant_text="old unrelated answer",
        )
    )

    assert intent.snapshots == []
    assert decision.intent.run_status == "not_run"
    assert decision.intent.prediction is None
    assert decision.intent.probabilities is None
    assert decision.intent.final == "new_task"
    assert decision.tier.final == "c2"
    assert decision.context_action == "reset"
    assert decision.history_turns_to_keep == 0
    assert decision.task_id == "task-1"
    assert next_state.payload() == {
        "task_id": "task-1",
        "tier": "c2",
        "turn_count": 1,
        "version": 1,
        "task_start_input_message_id": None,
        "schema_version": decision.schema_version,
    }
    snapshot, allowed = tier.calls[0]
    assert allowed == TIERS
    assert snapshot["active_task"] is False
    assert snapshot["task_user_history"] == []
    assert snapshot["previous_assistant_text"] is None
    assert snapshot["task_reset_mask"] is True


def test_continue_keeps_tier_context_and_skips_tier_classifier() -> None:
    intent = _ScriptedIntentClassifier([_prediction("continue")])
    tier = _ScriptedTierClassifier([_prediction("c1")])
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
        route_id_factory=_id_factory("route"),
        task_id_factory=_id_factory("task"),
    )
    first, state = router.decide(_request("request-1"))

    decision, next_state = router.decide(
        _request(
            "request-2",
            "继续完善",
            user_history=("请处理这个请求",),
            previous_assistant_text="第一轮回答",
        ),
        state,
    )

    assert decision.task_id == first.task_id
    assert decision.intent.source == "classifier"
    assert decision.intent.final == "continue"
    assert decision.tier.source == "not_run"
    assert decision.tier.run_status == "not_run"
    assert decision.tier.prediction is None
    assert decision.tier.probabilities is None
    assert decision.final_tier == "c1"
    assert decision.context_action == "keep"
    assert decision.history_turns_to_keep == 1
    assert next_state.version == 2
    assert next_state.turn_count == 2
    assert len(tier.calls) == 1
    json.dumps(decision.trace(provider="openrouter", model="model-c1"))


def test_explicit_redo_bypasses_intent_and_never_downgrades() -> None:
    intent = _ScriptedIntentClassifier([])
    tier = _ScriptedTierClassifier(
        [
            _prediction("c2"),
            _prediction("c0"),
        ]
    )
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
        route_id_factory=_id_factory("route"),
        task_id_factory=_id_factory("task"),
    )
    first, state = router.decide(_request("request-1"))

    decision, _ = router.decide(
        _request("request-2", control_event="regenerate"),
        state,
    )

    assert decision.task_id == first.task_id
    assert decision.intent.source == "rule"
    assert decision.intent.run_status == "not_run"
    assert decision.intent.final == "redo"
    assert tier.calls[1][1] == TIERS
    assert decision.tier.source == "fallback"
    assert decision.tier.run_status == "ran"
    assert decision.tier.prediction == "c0"
    assert decision.tier.reason == "redo_downgrade_blocked"
    assert decision.final_tier == "c2"
    assert decision.switch_reason == "redo_hold"
    assert decision.context_action == "keep"


def test_explicit_new_task_resets_task_and_old_task_features() -> None:
    intent = _ScriptedIntentClassifier([])
    tier = _ScriptedTierClassifier([_prediction("c3"), _prediction("c0")])
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
        route_id_factory=_id_factory("route"),
        task_id_factory=_id_factory("task"),
    )
    first, state = router.decide(_request("request-1"))

    decision, _ = router.decide(
        _request(
            "request-2",
            "新建任务：写另一份报告",
            user_history=("旧任务",),
            previous_assistant_text="旧任务回答",
        ),
        state,
    )

    assert decision.task_id != first.task_id
    assert decision.task_id == "task-2"
    assert decision.previous_tier == "c3"
    assert decision.final_tier == "c0"
    assert decision.context_action == "reset"
    assert decision.history_turns_to_keep == 0
    assert decision.intent.source == "rule"
    assert intent.snapshots == []
    snapshot, allowed = tier.calls[1]
    assert allowed == TIERS
    assert snapshot["active_task"] is False
    assert snapshot["current_tier"] is None
    assert snapshot["task_user_history"] == []
    assert snapshot["previous_assistant_text"] is None


def test_classifier_failures_use_conservative_fallbacks() -> None:
    intent = _ScriptedIntentClassifier([RuntimeError("local intent unavailable")])
    tier = _ScriptedTierClassifier(
        [
            RuntimeError("local tier unavailable"),
        ]
    )
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
        default_new_task_tier="c1",
    )

    first, state = router.decide(_request("request-1"))
    continued, _ = router.decide(_request("request-2", "继续"), state)

    assert first.final_tier == "c1"
    assert first.tier.source == "fallback"
    assert first.tier.run_status == "error"
    assert first.tier.prediction is None
    assert first.tier.probabilities is None
    assert continued.intent.source == "fallback"
    assert continued.intent.run_status == "error"
    assert continued.intent.final == "continue"
    assert continued.tier.run_status == "not_run"
    assert continued.final_tier == "c1"


def test_uncertain_redo_holds_current_tier() -> None:
    intent = _ScriptedIntentClassifier([])
    tier = _ScriptedTierClassifier(
        [
            _prediction("c1"),
            _prediction(
                "c3",
                confidence=0.45,
                probabilities={"c0": 0.05, "c1": 0.25, "c2": 0.25, "c3": 0.45},
            ),
        ]
    )
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
        tier_min_confidence=0.5,
    )
    _, state = router.decide(_request("request-1"))

    decision, _ = router.decide(
        _request("request-2", control_event="redo"),
        state,
    )

    assert decision.tier.source == "fallback"
    assert decision.tier.run_status == "ran"
    assert decision.tier.reason == "classifier_uncertain"
    assert decision.final_tier == "c1"


def test_seeded_random_mocks_are_replayable() -> None:
    def _run() -> list[tuple[str, str]]:
        router = FixedFourTierV2Router(mock_seed=20260826)
        results: list[tuple[str, str]] = []
        state: FixedFourTierTaskState | None = None
        for index in range(12):
            decision, state = router.decide(
                _request(
                    f"request-{index}",
                    "ordinary request",
                    control_event="new_task" if index == 0 else None,
                ),
                state,
            )
            results.append((decision.intent.final, decision.final_tier))
        return results

    assert _run() == _run()


def test_router_owns_no_process_local_session_state() -> None:
    tier = _ScriptedTierClassifier([_prediction("c0"), _prediction("c3")])
    router = FixedFourTierV2Router(tier_classifier=tier)

    first_a, state_a = router.decide(_request("a-1", session_id="a"))
    first_b, state_b = router.decide(_request("b-1", session_id="b"))

    assert first_a.final_tier == "c0"
    assert first_b.final_tier == "c3"
    assert state_a.task_id != state_b.task_id
    assert not hasattr(router, "state_snapshot")


@pytest.mark.parametrize(
    "control_event",
    ["redo", "regenerate", "retry_response"],
)
def test_redo_without_active_task_becomes_new_task(control_event: str) -> None:
    tier = _ScriptedTierClassifier([_prediction("c0")])
    router = FixedFourTierV2Router(tier_classifier=tier)

    decision = router.route(_request("request-1", control_event=control_event))

    assert decision.intent.source == "fallback"
    assert decision.intent.run_status == "not_run"
    assert decision.intent.prediction is None
    assert decision.intent.probabilities is None
    assert decision.intent.reason == "redo_without_active_task"
    assert decision.intent.final == "new_task"
    assert decision.context_action == "reset"


def test_feature_snapshot_is_bounded_and_trace_contains_only_safe_audit() -> None:
    intent = _ScriptedIntentClassifier([_prediction("continue")])
    tier = _ScriptedTierClassifier([_prediction("c2")])
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
        route_id_factory=_id_factory("route"),
        task_id_factory=_id_factory("task"),
        clock_ms=lambda: 123,
    )
    _, state = router.decide(
        _request(
            "request-1",
            input_message_id="input-start",
        )
    )
    current_text = "CURRENT-SECRET-" + ("x" * 2_100)
    long_history = "HISTORY-SECRET-" + ("y" * 2_100)
    previous_text = "PREVIOUS-SECRET-" + ("z" * 2_100)
    history = (
        "task start",
        "discarded one",
        "discarded two",
        long_history,
        "recent two",
        "recent three",
    )

    decision, _ = router.decide(
        _request(
            "request-2",
            current_text,
            user_history=history,
            previous_assistant_text=previous_text,
            input_message_id="input-current",
            attachment_count=2,
            attachment_modalities=("document", "image"),
        ),
        state,
    )

    snapshot = intent.snapshots[0]
    assert snapshot["feature_schema_version"] == FEATURE_SCHEMA_VERSION
    assert snapshot["feature_vector_dim"] == FEATURE_VECTOR_DIM
    assert snapshot["feature_vector_status"] == FEATURE_VECTOR_STATUS
    assert "feature_vector" not in snapshot
    assert len(snapshot["current_request"]) == 2_040
    assert snapshot["task_user_history"][0] == "task start"
    assert len(snapshot["task_user_history"]) == 4
    assert len(snapshot["task_user_history"][1]) == 2_040
    assert snapshot["truncated"] == {
        "current_request": True,
        "history": True,
        "history_window": True,
        "history_segments": [False, True, False, False],
        "previous_assistant": True,
    }
    assert snapshot["missing"] == {
        "context": False,
        "usage": True,
        "execution": True,
        "attachment_metadata": False,
    }
    assert snapshot["attachment_modalities"] == ["document", "image"]
    assert all(
        key not in snapshot
        for key in ("session_id", "request_id", "input_message_id", "control_event")
    )

    trace = decision.trace(provider="openrouter", model="model-c2")
    assert trace["feature_vector_dim"] == 413
    assert trace["feature_vector_status"] == "mock_not_materialized"
    feature_input = trace["feature_input"]
    assert feature_input["history_observed_count"] == 6
    assert feature_input["history_retained_count"] == 4
    assert feature_input["attachment_count"] == 2
    assert feature_input["attachment_modalities"] == ["document", "image"]
    assert feature_input["transcript_refs"] == {
        "input_message_id": "input-current",
        "task_start_input_message_id": "input-start",
    }
    assert (
        feature_input["content_hashes"]["current_request"]
        == hashlib.sha256(current_text.encode("utf-8")).hexdigest()
    )
    assert feature_input["content_hashes"]["task_user_history_segments"] == [
        hashlib.sha256(value.encode("utf-8")).hexdigest() for value in (history[0], *history[-3:])
    ]
    serialized_trace = json.dumps(trace, ensure_ascii=False)
    assert "CURRENT-SECRET" not in serialized_trace
    assert "HISTORY-SECRET" not in serialized_trace
    assert "PREVIOUS-SECRET" not in serialized_trace


@pytest.mark.parametrize(
    (
        "previous_metadata",
        "expected_usage",
        "expected_execution",
        "expected_missing",
    ),
    [
        (
            {
                "route_id": "route-previous",
                "execution_status": "succeeded",
                "error_code": None,
                "response_id": "response-previous",
                "attempt_ids": ["attempt-1"],
                "retry_count": 0,
            },
            None,
            {
                "route_id": "route-previous",
                "execution_status": "succeeded",
                "error_code": None,
                "response_id": "response-previous",
                "attempt_ids": ["attempt-1"],
                "retry_count": 0,
            },
            {"usage": True, "execution": False},
        ),
        (
            {
                "input_tokens": 10,
                "output_tokens": 20,
                "reasoning_tokens": 5,
                "cached_tokens": 3,
                "cache_write_tokens": 2,
                "cost_usd": 0.1,
            },
            {
                "input_tokens": 10,
                "output_tokens": 20,
                "reasoning_tokens": 5,
                "cached_tokens": 3,
                "cache_write_tokens": 2,
                "cost_usd": 0.1,
            },
            None,
            {"usage": False, "execution": True},
        ),
        (
            {
                "input_tokens": 10,
                "output_tokens": 20,
                "reasoning_tokens": 5,
                "cached_tokens": 3,
                "cache_write_tokens": 2,
                "route_id": "route-previous",
                "execution_status": "failed",
                "error_code": "provider_error",
                "response_id": None,
                "attempt_ids": ["attempt-1", "attempt-2"],
                "retry_count": 1,
            },
            {
                "input_tokens": 10,
                "output_tokens": 20,
                "reasoning_tokens": 5,
                "cached_tokens": 3,
                "cache_write_tokens": 2,
            },
            {
                "route_id": "route-previous",
                "execution_status": "failed",
                "error_code": "provider_error",
                "response_id": None,
                "attempt_ids": ["attempt-1", "attempt-2"],
                "retry_count": 1,
            },
            {"usage": False, "execution": False},
        ),
        (
            {
                # A partial historical turn_usage must not make the usage
                # bundle look complete after execution metadata is merged.
                "input_tokens": 10,
                "output_tokens": 20,
                "route_id": "route-previous",
                "execution_status": "succeeded",
                "error_code": None,
                "response_id": "response-previous",
                "attempt_ids": [],
                "retry_count": 0,
            },
            None,
            {
                "route_id": "route-previous",
                "execution_status": "succeeded",
                "error_code": None,
                "response_id": "response-previous",
                "attempt_ids": [],
                "retry_count": 0,
            },
            {"usage": True, "execution": False},
        ),
    ],
)
def test_previous_usage_and_execution_missing_masks_are_independent(
    previous_metadata: dict[str, Any],
    expected_usage: dict[str, Any] | None,
    expected_execution: dict[str, Any] | None,
    expected_missing: dict[str, bool],
) -> None:
    intent = _ScriptedIntentClassifier([_prediction("continue")])
    tier = _ScriptedTierClassifier([_prediction("c1")])
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
    )
    _, state = router.decide(_request("request-1", input_message_id="task-start"))

    decision, _ = router.decide(
        _request(
            "request-2",
            user_history=("task start",),
            previous_assistant_text="previous answer",
            previous_assistant_usage=previous_metadata,
        ),
        state,
    )

    snapshot = intent.snapshots[0]
    assert snapshot["previous_assistant_usage"] == expected_usage
    assert snapshot["previous_execution"] == expected_execution
    assert snapshot["missing"]["usage"] is expected_missing["usage"]
    assert snapshot["missing"]["execution"] is expected_missing["execution"]
    assert decision.feature_input_audit.missing_usage is expected_missing["usage"]
    assert decision.feature_input_audit.missing_execution is expected_missing["execution"]


def test_new_task_reset_mask_keeps_missing_flags_without_old_task_leakage() -> None:
    intent = _ScriptedIntentClassifier([])
    tier = _ScriptedTierClassifier([_prediction("c2"), _prediction("c3")])
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
    )
    _, state = router.decide(_request("request-1", input_message_id="old-start"))
    long_old_history = "old" * 1_000

    decision, _ = router.decide(
        _request(
            "request-2",
            "新建任务：" + ("n" * 2_100),
            user_history=("old start", "drop", "drop2", long_old_history, "r2", "r3"),
            previous_assistant_text="previous" * 300,
            input_message_id="new-start",
        ),
        state,
    )

    snapshot, _ = tier.calls[1]
    assert snapshot["task_reset_mask"] is True
    assert snapshot["task_user_history"] == []
    assert snapshot["previous_assistant_text"] is None
    assert snapshot["previous_assistant_usage"] is None
    assert snapshot["previous_execution"] is None
    assert snapshot["missing"] == {
        "context": False,
        "usage": True,
        "execution": True,
        "attachment_metadata": False,
    }
    assert snapshot["truncated"] == {
        "current_request": True,
        "history": False,
        "history_window": False,
        "history_segments": [],
        "previous_assistant": False,
    }
    assert decision.feature_input_audit.truncated_history is True


def test_attachment_metadata_bundle_is_normalized_all_or_nothing() -> None:
    assert normalize_attachment_modalities([]) == ()
    assert normalize_attachment_modalities(
        [
            {"mime": "application/pdf", "name": "private-name.pdf"},
            {"mime_type": "image/png", "content": "must-not-enter-routing"},
            {"content_type": "application/zip"},
            {"type": "opaque-provider-file"},
        ]
    ) == ("document", "image", "archive", "other")
    assert normalize_attachment_modalities([{"mime": "image/png"}, {}]) is None


def test_incomplete_attachment_metadata_zeros_bundle_and_sets_missing_mask() -> None:
    intent = _ScriptedIntentClassifier([])
    tier = _ScriptedTierClassifier([_prediction("c1")])
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
    )

    decision, _ = router.decide(
        _request(
            "request-with-incomplete-attachments",
            attachment_count=2,
            attachment_modalities=None,
        )
    )

    snapshot, _ = tier.calls[0]
    assert snapshot["attachment_count"] == 0
    assert snapshot["attachment_modalities"] == []
    assert snapshot["missing"]["attachment_metadata"] is True
    assert decision.feature_input_audit.attachment_modalities == ()
    assert decision.feature_input_audit.missing_attachment_metadata is True
    traced = decision.trace()["feature_input"]
    assert traced["attachment_count"] == 0
    assert traced["attachment_modalities"] == []
    assert traced["missing"]["attachment_metadata"] is True


def test_empty_attachment_metadata_bundle_is_complete() -> None:
    request = _request("request-without-attachments", attachment_modalities=None)

    assert request.attachment_count == 0
    assert request.attachment_modalities == ()


def test_partial_attachment_metadata_bundle_is_treated_as_missing() -> None:
    request = _request(
        "request-with-partial-attachments",
        attachment_count=2,
        attachment_modalities=("image",),
    )

    assert request.attachment_modalities is None


def test_explicit_control_and_request_identity_never_enter_classifier_snapshot() -> None:
    intent = _ScriptedIntentClassifier([])
    tier = _ScriptedTierClassifier([_prediction("c1"), _prediction("c2")])
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
    )
    _, state = router.decide(_request("request-1", input_message_id="input-1"))

    router.decide(
        _request(
            "request-2",
            control_event="regenerate",
            input_message_id="input-2",
            session_id="another-session",
            user_history=("same task",),
        ),
        state,
    )

    snapshot, _ = tier.calls[1]
    assert all(
        key not in snapshot
        for key in ("session_id", "request_id", "input_message_id", "control_event")
    )


def test_mock_result_is_independent_of_session_queue_and_message_identity() -> None:
    state = FixedFourTierTaskState(
        task_id="durable-task",
        tier="c1",
        turn_count=3,
        version=3,
        task_start_input_message_id="task-start",
    )

    def run(session_id: str, request_id: str, input_message_id: str) -> FixedFourTierDecision:
        router = FixedFourTierV2Router(
            mock_seed=20260826,
            route_id_factory=lambda: "route",
            task_id_factory=lambda: "new-task",
            clock_ms=lambda: 123,
        )
        decision, _ = router.decide(
            _request(
                request_id,
                "identical semantic input",
                session_id=session_id,
                input_message_id=input_message_id,
                user_history=("task start", "latest"),
                previous_assistant_text="previous",
                previous_assistant_usage={"status": "success"},
            ),
            state,
        )
        return decision

    first = run("session-a", "queue-request-a", "message-a")
    second = run("session-b", "queue-request-b", "message-b")

    assert first.intent == second.intent
    assert first.tier == second.tier
    assert first.final_tier == second.final_tier
    assert (
        first.feature_input_audit.classifier_snapshot_hash
        == second.feature_input_audit.classifier_snapshot_hash
    )
    assert first.input_snapshot_hash != second.input_snapshot_hash


def test_state_payload_round_trip_is_strict() -> None:
    state = FixedFourTierTaskState(
        task_id="task-a",
        tier="c2",
        turn_count=4,
        version=5,
        task_start_input_message_id="input-a",
    )
    payload = state.payload()

    assert FixedFourTierTaskState.from_payload(payload) == state

    for field_name, value in (
        ("schema_version", "old"),
        ("tier", "C2"),
        ("turn_count", True),
        ("version", -1),
        ("task_start_input_message_id", ""),
    ):
        invalid = dict(payload)
        invalid[field_name] = value
        with pytest.raises(ValueError):
            FixedFourTierTaskState.from_payload(invalid)
    with pytest.raises(ValueError, match="shape"):
        FixedFourTierTaskState.from_payload({**payload, "unexpected": True})


def test_decision_trace_round_trip_and_corruption_rejection() -> None:
    tier = _ScriptedTierClassifier([_prediction("c2")])
    router = FixedFourTierV2Router(
        tier_classifier=tier,
        mock_seed=7,
        route_id_factory=lambda: "route-a",
        task_id_factory=lambda: "task-a",
        clock_ms=lambda: 123,
    )
    decision, _ = router.decide(
        _request(
            "request-a",
            input_message_id="input-a",
            attachment_count=1,
        )
    )
    trace = decision.trace(provider="openrouter", model="deepseek/example")

    assert trace["mode"] == "four_tier_mapping"
    assert FixedFourTierDecision.from_trace(trace) == decision

    corruptions: list[tuple[tuple[str, ...], object]] = [
        (("mode",), "fixed_four_tier_v2"),
        (("mode",), "fixed-four-tier-v2"),
        (("mode",), "router_dynamic"),
        (("feature_vector_status",), "materialized"),
        (("intent", "source"), "not_run"),
        (("intent", "final"), "unknown"),
        (("tier", "run_status"), "not_run"),
        (("tier", "confidence"), 0.5),
        (("switched",), "false"),
        (("context_action",), "keep"),
        (("input_snapshot_hash",), "not-a-hash"),
        (("feature_input", "missing", "usage"), 0),
        (("feature_input", "attachment_modalities"), ["not-a-modality"]),
        (("feature_input", "attachment_count"), -1),
        (("feature_input", "content_hashes", "current_request"), "bad"),
    ]
    for path, value in corruptions:
        invalid = copy.deepcopy(trace)
        target: dict[str, Any] = invalid
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValueError):
            FixedFourTierDecision.from_trace(invalid)

    invalid_probabilities = copy.deepcopy(trace)
    del invalid_probabilities["tier"]["probabilities"]["c3"]
    with pytest.raises(ValueError, match="probability labels"):
        FixedFourTierDecision.from_trace(invalid_probabilities)


def test_malformed_classifier_probabilities_fail_safe_without_fake_audit_values() -> None:
    tier = _ScriptedTierClassifier(
        [
            _prediction(
                "c2",
                confidence=0.8,
                probabilities={"c0": float("nan"), "c1": 0.1, "c2": 0.8, "c3": 0.1},
            )
        ]
    )
    router = FixedFourTierV2Router(
        tier_classifier=tier,
        default_new_task_tier="c1",
    )

    decision = router.route(_request("request-a"))

    assert decision.tier.source == "fallback"
    assert decision.tier.run_status == "error"
    assert decision.tier.reason == "invalid_classifier_result"
    assert decision.tier.probabilities is None
    assert decision.final_tier == "c1"
