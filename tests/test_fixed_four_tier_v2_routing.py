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
    SCHEMA_VERSION,
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
    backend: str = "injected"
    identity: Mapping[str, Any] | None = None
    feature_schema_version: str = FEATURE_SCHEMA_VERSION
    feature_vector_dim: int | None = FEATURE_VECTOR_DIM
    feature_vector_status: str = FEATURE_VECTOR_STATUS

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
    backend: str = "injected"
    identity: Mapping[str, Any] | None = None
    feature_schema_version: str = FEATURE_SCHEMA_VERSION
    feature_vector_dim: int | None = FEATURE_VECTOR_DIM
    feature_vector_status: str = FEATURE_VECTOR_STATUS

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
    task_anchor: str | None = None,
    previous_assistant_text: str | None = None,
    previous_assistant_usage: Mapping[str, Any] | None = None,
    previous_outcome: str = "unknown",
    route_history: tuple[Mapping[str, Any], ...] = (),
    context: Mapping[str, Any] | None = None,
    tool_state: Mapping[str, Any] | None = None,
    attachments: tuple[Mapping[str, Any], ...] = (),
    input_message_id: str | None = None,
    quality_failure_reason: str | None = None,
    quality_retry_budget_remaining: int = 0,
    quality_retry_already_used: bool = False,
    attachment_count: int = 0,
    attachment_modalities: tuple[str, ...] | None = None,
) -> RoutingRequest:
    return RoutingRequest(
        session_id=session_id,
        request_id=request_id,
        message=message,
        input_message_id=input_message_id,
        quality_failure_reason=quality_failure_reason,
        quality_retry_budget_remaining=quality_retry_budget_remaining,
        quality_retry_already_used=quality_retry_already_used,
        control_event=control_event,
        task_anchor=task_anchor,
        user_history=user_history,
        previous_assistant_text=previous_assistant_text,
        previous_assistant_usage=previous_assistant_usage,
        previous_outcome=previous_outcome,
        route_history=route_history,
        context=context,
        tool_state=tool_state,
        attachments=attachments,
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


def _registered_identity(
    *,
    feature_schema_version: str = "lightgbm_380.v1",
    registry_status: str = "VALIDATED",
) -> dict[str, str]:
    model_type = "bert" if feature_schema_version == "bert_text88.v1" else "lightgbm"
    return {
        "schema_version": "local_runner_identity.v2",
        "model_set_id": "router-production-a1",
        "model_manifest_hash": "sha256:" + ("a" * 64),
        "artifact_closure_hash": "sha256:" + ("b" * 64),
        "runner_digest": "sha256:" + ("c" * 64),
        "environment_digest": "sha256:" + ("d" * 64),
        "model_type": model_type,
        "execution_mode": "native_embedded",
        "registry_status": registry_status,
        "input_schema_version": feature_schema_version,
    }


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


@pytest.mark.parametrize(
    ("predicted_tier", "final_tier", "source"),
    [("c0", "c1", "fallback"), ("c1", "c1", "classifier"), ("c3", "c3", "classifier")],
)
def test_continue_reclassifies_tier_without_downgrade_and_keeps_context(
    predicted_tier: str,
    final_tier: str,
    source: str,
) -> None:
    intent = _ScriptedIntentClassifier([_prediction("continue")])
    tier = _ScriptedTierClassifier([_prediction("c1"), _prediction(predicted_tier)])
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
        route_id_factory=_id_factory("route"),
        task_id_factory=_id_factory("task"),
    )
    first, state = router.decide(_request("request-1", input_message_id="input-1"))
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
    assert decision.tier.source == source
    assert decision.tier.run_status == "ran"
    assert decision.tier.prediction == predicted_tier
    assert decision.tier.probabilities is not None
    assert decision.tier_snapshot_hash is not None
    assert decision.final_tier == final_tier
    assert decision.switch_reason == ("continue_upgrade" if final_tier != "c1" else "continue_hold")
    assert decision.context_action == "keep"
    assert decision.history_turns_to_keep == 1
    assert next_state.version == 2
    assert next_state.turn_count == 2
    assert next_state.task_start_input_message_id == "input-1"
    assert len(tier.calls) == 2
    snapshot, allowed = tier.calls[1]
    assert allowed == TIERS
    assert snapshot.get("task_reset_mask", False) is False
    assert snapshot["task_user_history"] == ["请处理这个请求"]
    assert snapshot["previous_assistant_text"] == "第一轮回答"
    trace = json.loads(json.dumps(decision.trace(provider="openrouter", model="model-c1")))
    assert FixedFourTierDecision.from_trace(trace) == decision


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
    assert continued.tier.run_status == "error"
    assert continued.tier.source == "fallback"
    assert continued.final_tier == "c1"


def test_classifier_authorization_failure_is_not_downgraded_to_fallback() -> None:
    class AuthorizationError(RuntimeError):
        fail_closed = True

    router = FixedFourTierV2Router(
        tier_classifier=_ScriptedTierClassifier([AuthorizationError("revoked")]),
    )

    with pytest.raises(AuthorizationError, match="revoked"):
        router.decide(_request("request-revoked"))


def test_low_confidence_redo_uses_argmax_and_upgrades() -> None:
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

    assert decision.tier.source == "classifier"
    assert decision.tier.run_status == "ran"
    assert decision.tier.reason == "classifier_selected"
    assert decision.final_tier == "c3"


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
    assert snapshot["task_user_history"][0] == "discarded two"
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
        hashlib.sha256(value.encode("utf-8")).hexdigest() for value in history[-4:]
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


def test_new_task_snapshot_preserves_complete_cross_task_router_input() -> None:
    intent = _ScriptedIntentClassifier([])
    tier = _ScriptedTierClassifier([_prediction("c2"), _prediction("c3")])
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
    )
    _, state = router.decide(_request("request-1"))
    current_request = "新建任务：" + ("原文" * 1_100)
    history = (
        "任务甲-较早",
        "任务甲-结束",
        "任务乙-开始",
        "任务乙-结束",
        "任务丙-开始",
        "任务丙-最近",
    )
    previous_usage = {
        "input_tokens": 101,
        "output_tokens": 202,
        "reasoning_tokens": 33,
        "cached_tokens": 44,
        "cache_write_tokens": 55,
        "cost_usd": 0.125,
    }
    route_history = (
        {"intent": "continue", "tier": "C1"},
        {"intent": "redo", "tier": "C2"},
    )
    context = {"surface": "cli", "workspace_kind": "git"}
    tool_state = {"available": ["shell", "browser"], "last_error": None}
    attachments = (
        {"mime_type": "application/pdf", "size_bytes": 123},
        {"mime_type": "image/png", "size_bytes": 456},
    )

    router.decide(
        _request(
            "request-2",
            current_request,
            task_anchor="跨任务保留的锚点",
            user_history=history,
            previous_assistant_text="上一任务的完整回答",
            previous_assistant_usage=previous_usage,
            previous_outcome="failure",
            route_history=route_history,
            context=context,
            tool_state=tool_state,
            attachments=attachments,
            attachment_count=2,
            attachment_modalities=("document", "image"),
        ),
        state,
    )

    snapshot, _ = tier.calls[1]
    assert snapshot["task_reset_mask"] is True
    assert snapshot["task_user_history"] == []
    assert snapshot["previous_assistant_text"] is None
    assert len(snapshot["current_request"]) == 2_040
    assert snapshot["router_input"] == {
        "current_request": current_request,
        "task_anchor": "跨任务保留的锚点",
        "history_user": list(history[-4:]),
        "previous_answer": "上一任务的完整回答",
        "previous_usage": previous_usage,
        "previous_outcome": "failure",
        "active_route_tier": "C2",
        "route_history": [dict(value) for value in route_history],
        "context": context,
        "tool_state": tool_state,
        "attachments": [dict(value) for value in attachments],
    }


def test_registered_new_task_without_state_audits_canonical_cross_task_text() -> None:
    identity = _registered_identity()
    classifier_metadata = {
        "backend": "registered_model",
        "identity": identity,
        "feature_schema_version": "lightgbm_380.v1",
        "feature_vector_dim": 380,
        "feature_vector_status": "materialized",
    }
    intent = _ScriptedIntentClassifier([], **classifier_metadata)
    tier = _ScriptedTierClassifier([_prediction("c2")], **classifier_metadata)
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
        route_id_factory=lambda: "route-canonical-audit",
        task_id_factory=lambda: "task-canonical-audit",
        clock_ms=lambda: 123,
    )
    history = (
        "任务甲-较早",
        "任务甲-结束",
        "任务乙-开始",
        "任务乙-结束",
        "任务丙-开始",
        "任务丙-最近",
    )
    previous_answer = "跨任务上一回答" + ("答" * 2_100)
    request = _request(
        "request-canonical-audit",
        "新任务请求" + ("问" * 2_100),
        user_history=history,
        previous_assistant_text=previous_answer,
    )

    decision = router.route(request)

    snapshot, _ = tier.calls[0]
    router_input = snapshot["router_input"]
    assert router_input["history_user"] == list(history[-4:])
    assert router_input["previous_answer"] == previous_answer
    audit = decision.feature_input_audit
    assert audit.input_contract == "canonical_router_input"
    assert audit.history_observed_count == len(history)
    assert audit.history_retained_count == 4
    assert audit.history_content_hashes == tuple(
        hashlib.sha256(value.encode("utf-8")).hexdigest() for value in history[-4:]
    )
    assert (
        audit.previous_assistant_content_hash
        == hashlib.sha256(previous_answer.encode("utf-8")).hexdigest()
    )
    assert (
        audit.classifier_snapshot_hash
        == hashlib.sha256(
            json.dumps(
                router_input,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    assert audit.truncated_current_request is False
    assert audit.truncated_history is True
    assert audit.truncated_history_window is True
    assert audit.truncated_history_segments == (False, False, False, False)
    assert audit.truncated_previous_assistant is False
    content_hashes = decision.trace()["feature_input"]["content_hashes"]
    assert set(content_hashes) == {
        "current_request",
        "history_user_segments",
        "history_user_aggregate",
        "previous_answer",
    }
    assert "task_user_history_segments" not in content_hashes


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
    assert trace["schema_version"] == SCHEMA_VERSION
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


def test_legacy_mock_v2_decision_trace_remains_rehydratable() -> None:
    router = FixedFourTierV2Router(
        mock_seed=7,
        route_id_factory=lambda: "legacy-route",
        task_id_factory=lambda: "legacy-task",
        clock_ms=lambda: 123,
    )
    trace = router.route(_request("legacy-request")).trace()
    trace["schema_version"] = "fixed-four-tier-v2-mock-v2"
    trace.pop("classifier_backend")
    trace.pop("classifier_identity")
    trace.pop("quality_escalation_reason")
    trace.pop("quality_escalation_used")

    restored = FixedFourTierDecision.from_trace(trace)

    assert restored.schema_version == "fixed-four-tier-v2-mock-v2"
    assert restored.classifier_backend == "random_mock"
    assert restored.classifier_identity is None
    assert restored.feature_input_audit.input_contract == "legacy_mock_snapshot"
    assert set(trace["feature_input"]["content_hashes"]) == {
        "current_request",
        "task_user_history_segments",
        "task_user_history_aggregate",
        "previous_assistant",
    }


@pytest.mark.parametrize(
    ("feature_schema_version", "feature_vector_dim"),
    [("lightgbm_380.v1", 380), ("bert_text88.v1", 88)],
)
@pytest.mark.parametrize("registry_status", ["VALIDATED", "CANDIDATE"])
def test_materialized_model_trace_identity_and_feature_schema_round_trip(
    feature_schema_version: str,
    feature_vector_dim: int,
    registry_status: str,
) -> None:
    identity = _registered_identity(
        feature_schema_version=feature_schema_version,
        registry_status=registry_status,
    )
    classifier_metadata = {
        "backend": "registered_model",
        "identity": identity,
        "feature_schema_version": feature_schema_version,
        "feature_vector_dim": feature_vector_dim,
        "feature_vector_status": "materialized",
    }
    intent = _ScriptedIntentClassifier([], **classifier_metadata)
    tier = _ScriptedTierClassifier([_prediction("c2")], **classifier_metadata)
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
        route_id_factory=lambda: "route-materialized",
        task_id_factory=lambda: "task-materialized",
        clock_ms=lambda: 123,
    )

    decision = router.route(_request("request-materialized"))
    trace = json.loads(json.dumps(decision.trace(provider="local", model="router-production-a1")))

    assert trace["schema_version"] == SCHEMA_VERSION
    assert trace["classifier_backend"] == "registered_model"
    assert trace["classifier_identity"] == identity
    assert trace["feature_schema_version"] == feature_schema_version
    assert trace["feature_vector_dim"] == feature_vector_dim
    assert trace["feature_vector_status"] == "materialized"
    assert trace["feature_input"]["content_hashes"].keys() == {
        "current_request",
        "history_user_segments",
        "history_user_aggregate",
        "previous_answer",
    }
    assert FixedFourTierDecision.from_trace(trace) == decision

    missing_identity = copy.deepcopy(trace)
    missing_identity["classifier_identity"] = None
    with pytest.raises(ValueError, match="runtime identity"):
        FixedFourTierDecision.from_trace(missing_identity)


def test_registered_model_trace_rejects_feature_and_identity_tampering() -> None:
    identity = _registered_identity()
    classifier_metadata = {
        "backend": "registered_model",
        "identity": identity,
        "feature_schema_version": "lightgbm_380.v1",
        "feature_vector_dim": 380,
        "feature_vector_status": "materialized",
    }
    router = FixedFourTierV2Router(
        intent_classifier=_ScriptedIntentClassifier([], **classifier_metadata),
        tier_classifier=_ScriptedTierClassifier([_prediction("c2")], **classifier_metadata),
        route_id_factory=lambda: "route-materialized",
        task_id_factory=lambda: "task-materialized",
        clock_ms=lambda: 123,
    )
    trace = router.route(_request("request-materialized")).trace()

    corruptions: list[tuple[tuple[str, ...], object]] = [
        (("feature_schema_version",), "unknown_features.v1"),
        (("feature_vector_dim",), 88),
        (("feature_vector_status",), "mock_not_materialized"),
        (("classifier_identity", "schema_version"), "local_runner_identity.v1"),
        (("classifier_identity", "model_set_id"), ""),
        (("classifier_identity", "model_type"), "bert"),
        (
            ("classifier_identity", "execution_mode"),
            "native_isolated_subprocess",
        ),
        (("classifier_identity", "registry_status"), "DEPRECATED"),
        (("classifier_identity", "input_schema_version"), "bert_text88.v1"),
    ]
    corruptions.extend(
        (("classifier_identity", field_name), "sha256:" + ("f" * 63))
        for field_name in (
            "model_manifest_hash",
            "artifact_closure_hash",
            "runner_digest",
            "environment_digest",
        )
    )
    for path, value in corruptions:
        invalid = copy.deepcopy(trace)
        target: dict[str, Any] = invalid
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValueError):
            FixedFourTierDecision.from_trace(invalid)

    for field_name in identity:
        missing_field = copy.deepcopy(trace)
        del missing_field["classifier_identity"][field_name]
        with pytest.raises(ValueError, match="runtime identity"):
            FixedFourTierDecision.from_trace(missing_field)

    legacy_audit = copy.deepcopy(trace)
    content_hashes = legacy_audit["feature_input"]["content_hashes"]
    content_hashes["task_user_history_segments"] = content_hashes.pop("history_user_segments")
    content_hashes["task_user_history_aggregate"] = content_hashes.pop("history_user_aggregate")
    content_hashes["previous_assistant"] = content_hashes.pop("previous_answer")
    truncated = legacy_audit["feature_input"]["truncated"]
    truncated["previous_assistant"] = truncated.pop("previous_answer")
    with pytest.raises(ValueError, match="canonical RouterInput audit"):
        FixedFourTierDecision.from_trace(legacy_audit)


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


def test_low_confidence_and_small_margin_select_argmax_for_both_heads() -> None:
    intent = _ScriptedIntentClassifier(
        [
            _prediction(
                "new_task",
                confidence=0.34,
                probabilities={"continue": 0.33, "redo": 0.33, "new_task": 0.34},
            )
        ]
    )
    tier = _ScriptedTierClassifier(
        [
            _prediction("c1"),
            _prediction(
                "c2",
                confidence=0.26,
                probabilities={"c0": 0.25, "c1": 0.25, "c2": 0.26, "c3": 0.24},
            ),
        ]
    )
    router = FixedFourTierV2Router(
        intent_classifier=intent,
        tier_classifier=tier,
        intent_min_confidence=1.0,
        tier_min_confidence=1.0,
        min_margin=1.0,
        task_id_factory=_id_factory("task"),
    )
    first, state = router.decide(_request("first"))
    decision = router.route(_request("second", "unrelated request"), state)

    assert decision.intent.source == "classifier"
    assert decision.intent.final == "new_task"
    assert decision.tier.source == "classifier"
    assert decision.final_tier == "c2"
    assert decision.task_id != first.task_id
    assert decision.context_action == "reset"
    assert FixedFourTierDecision.from_trace(decision.trace()) == decision


def test_tied_probability_argmax_uses_fixed_class_order() -> None:
    # Mapping insertion order must not decide tied classifications.
    intent = _ScriptedIntentClassifier(
        [
            _prediction(
                "continue",
                confidence=1 / 3,
                probabilities={"new_task": 1 / 3, "redo": 1 / 3, "continue": 1 / 3},
            )
        ]
    )
    tier = _ScriptedTierClassifier(
        [
            _prediction(
                "c0",
                confidence=0.25,
                probabilities={"c3": 0.25, "c2": 0.25, "c1": 0.25, "c0": 0.25},
            ),
            _prediction(
                "c2",
                confidence=0.4,
                probabilities={"c3": 0.4, "c2": 0.4, "c1": 0.1, "c0": 0.1},
            ),
        ]
    )
    router = FixedFourTierV2Router(intent_classifier=intent, tier_classifier=tier)
    first, state = router.decide(_request("first"))
    decision = router.route(_request("second"), state)

    assert first.final_tier == "c0"
    assert decision.intent.final == "continue"
    assert decision.intent.source == "classifier"
    assert decision.final_tier == "c2"
    assert decision.tier.source == "classifier"
    assert FixedFourTierDecision.from_trace(decision.trace()) == decision


def test_ignored_confidence_thresholds_do_not_change_policy_hash() -> None:
    def route(
        intent_threshold: float, tier_threshold: float, margin: float
    ) -> FixedFourTierDecision:
        return FixedFourTierV2Router(
            mock_seed=7,
            intent_min_confidence=intent_threshold,
            tier_min_confidence=tier_threshold,
            min_margin=margin,
            policy_config={
                "intent_min_confidence": intent_threshold,
                "tier_min_confidence": tier_threshold,
                "min_margin": margin,
            },
        ).route(_request("same-request"))

    assert route(0.0, 0.0, 0.0).policy_hash == route(1.0, 1.0, 1.0).policy_hash


@pytest.mark.parametrize("reason", ["explicit_correction", "validation_failure", "no_progress"])
def test_quality_failure_adds_one_tier_from_current_and_keeps_context(reason: str) -> None:
    router = FixedFourTierV2Router(
        intent_classifier=_ScriptedIntentClassifier([_prediction("continue")]),
        tier_classifier=_ScriptedTierClassifier([_prediction("c1"), _prediction("c0")]),
    )
    first, state = router.decide(_request("first", input_message_id="anchor"))
    decision, next_state = router.decide(
        _request(
            "second",
            quality_failure_reason=reason,
            quality_retry_budget_remaining=1,
            user_history=("original request",),
        ),
        state,
    )

    assert decision.final_tier == "c2"
    assert decision.task_id == first.task_id
    assert decision.context_action == "keep"
    assert next_state.task_start_input_message_id == "anchor"
    trace = decision.trace()
    assert trace["quality_escalation_used"] is True
    assert trace["quality_escalation_reason"] == reason
    assert FixedFourTierDecision.from_trace(trace) == decision


@pytest.mark.parametrize(
    ("current", "prediction", "budget", "used", "reason", "expected", "retry_used"),
    [
        ("c1", "c1", 0, False, "validation_failure", "c1", False),
        ("c1", "c1", 1, True, "validation_failure", "c1", False),
        ("c3", "c0", 1, False, "validation_failure", "c3", False),
        ("c1", "c1", 1, False, None, "c1", False),
        ("c0", "c2", 1, False, "validation_failure", "c2", False),
    ],
)
def test_quality_retry_respects_budget_once_top_tier_and_does_not_stack(
    current: str,
    prediction: str,
    budget: int,
    used: bool,
    reason: str | None,
    expected: str,
    retry_used: bool,
) -> None:
    router = FixedFourTierV2Router(
        intent_classifier=_ScriptedIntentClassifier([_prediction("continue")]),
        tier_classifier=_ScriptedTierClassifier([_prediction(current), _prediction(prediction)]),
    )
    _, state = router.decide(_request("first"))
    decision = router.route(
        _request(
            "second",
            quality_failure_reason=reason,
            quality_retry_budget_remaining=budget,
            quality_retry_already_used=used,
        ),
        state,
    )

    assert decision.final_tier == expected
    assert decision.trace()["quality_escalation_used"] is retry_used
    assert FixedFourTierDecision.from_trace(decision.trace()) == decision


def test_new_task_quality_signal_does_not_upgrade_unrelated_task() -> None:
    router = FixedFourTierV2Router(
        tier_classifier=_ScriptedTierClassifier([_prediction("c1"), _prediction("c0")]),
    )
    _, state = router.decide(_request("first"))
    decision = router.route(
        _request(
            "second",
            control_event="new_task",
            quality_failure_reason="explicit_correction",
            quality_retry_budget_remaining=1,
        ),
        state,
    )

    assert decision.final_tier == "c0"
    assert decision.context_action == "reset"
    assert decision.trace()["quality_escalation_used"] is False


def test_quality_retry_trace_rejects_invalid_or_forged_usage() -> None:
    router = FixedFourTierV2Router(
        tier_classifier=_ScriptedTierClassifier([_prediction("c1")]),
    )
    trace = router.route(_request("first")).trace()
    for patch in (
        {"quality_escalation_used": "true"},
        {"quality_escalation_used": True, "quality_escalation_reason": "validation_failure"},
        {"quality_escalation_reason": "rate_limit"},
    ):
        with pytest.raises(ValueError):
            FixedFourTierDecision.from_trace({**trace, **patch})


def test_legacy_v3_continue_trace_remains_readable_with_original_tier_audit() -> None:
    router = FixedFourTierV2Router(
        intent_classifier=_ScriptedIntentClassifier([_prediction("continue")]),
        tier_classifier=_ScriptedTierClassifier([_prediction("c1"), _prediction("c1")]),
    )
    _, state = router.decide(_request("first"))
    trace = router.route(_request("second"), state).trace()
    trace["schema_version"] = "fixed-four-tier-v2-v3"
    trace["tier_snapshot_hash"] = None
    trace["tier"] = {
        "source": "not_run",
        "run_status": "not_run",
        "prediction": None,
        "probabilities": None,
        "confidence": None,
        "final": "c1",
        "reason": "continue_keeps_current_tier",
        "version": None,
    }
    trace.pop("quality_escalation_used", None)
    trace.pop("quality_escalation_reason", None)

    restored = FixedFourTierDecision.from_trace(trace)
    assert restored.schema_version == "fixed-four-tier-v2-v3"
    assert restored.tier.run_status == "not_run"
    assert restored.context_action == "keep"

    forged = copy.deepcopy(trace)
    forged["final_tier"] = "c2"
    forged["tier"]["final"] = "c2"
    forged["switched"] = True
    forged["switch_reason"] = "continue_upgrade"
    with pytest.raises(ValueError):
        FixedFourTierDecision.from_trace(forged)


@pytest.mark.parametrize("field", ["quality_escalation_reason", "quality_escalation_used"])
def test_v4_trace_requires_both_quality_audit_fields(field: str) -> None:
    trace = FixedFourTierV2Router(mock_seed=7).route(_request("first")).trace()
    del trace[field]

    with pytest.raises(ValueError):
        FixedFourTierDecision.from_trace(trace)


def test_quality_escalation_cannot_replace_a_higher_classifier_prediction() -> None:
    router = FixedFourTierV2Router(
        intent_classifier=_ScriptedIntentClassifier([_prediction("continue")]),
        tier_classifier=_ScriptedTierClassifier([_prediction("c1"), _prediction("c0")]),
    )
    _, state = router.decide(_request("first"))
    trace = router.route(
        _request(
            "second",
            quality_failure_reason="validation_failure",
            quality_retry_budget_remaining=1,
        ),
        state,
    ).trace()
    trace["tier"]["prediction"] = "c3"
    trace["tier"]["probabilities"] = {"c0": 0.0, "c1": 0.0, "c2": 0.0, "c3": 1.0}
    trace["tier"]["confidence"] = 1.0

    with pytest.raises(ValueError):
        FixedFourTierDecision.from_trace(trace)


@pytest.mark.parametrize(
    "patch",
    [
        {"quality_failure_reason": "rate_limit"},
        {"quality_retry_budget_remaining": True},
        {"quality_retry_budget_remaining": -1},
        {"quality_retry_budget_remaining": 1.0},
        {"quality_retry_already_used": "false"},
    ],
)
def test_quality_control_rejects_invalid_types_and_infrastructure_reasons(
    patch: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        RoutingRequest(session_id="session-a", request_id="request-a", message="request", **patch)


def test_quality_control_is_audited_without_entering_classifier_features() -> None:
    intent = _ScriptedIntentClassifier([_prediction("continue")])
    tier = _ScriptedTierClassifier([_prediction("c1"), _prediction("c1")])
    router = FixedFourTierV2Router(intent_classifier=intent, tier_classifier=tier)
    _, state = router.decide(_request("first"))
    decision = router.route(
        _request(
            "second",
            quality_failure_reason="validation_failure",
            quality_retry_budget_remaining=1,
        ),
        state,
    )

    assert decision.trace()["quality_escalation_used"] is True
    for snapshot in (intent.snapshots[0], tier.calls[1][0]):
        assert "quality_escalation_control" not in snapshot
        assert "quality_failure_reason" not in snapshot
        assert "quality_retry_budget_remaining" not in snapshot
        assert "quality_retry_already_used" not in snapshot


def test_v4_continue_rejects_skipped_tier_classification() -> None:
    router = FixedFourTierV2Router(
        intent_classifier=_ScriptedIntentClassifier([_prediction("continue")]),
        tier_classifier=_ScriptedTierClassifier([_prediction("c1"), _prediction("c1")]),
    )
    _, state = router.decide(_request("first"))
    trace = router.route(_request("second"), state).trace()
    trace["tier"] = {
        "source": "not_run",
        "run_status": "not_run",
        "prediction": None,
        "probabilities": None,
        "confidence": None,
        "final": "c1",
        "reason": "continue_keeps_current_tier",
        "version": None,
    }
    trace["tier_snapshot_hash"] = None

    with pytest.raises(ValueError):
        FixedFourTierDecision.from_trace(trace)


@pytest.mark.parametrize("schema", ["fixed-four-tier-v2-v3", "fixed-four-tier-v2-mock-v2"])
@pytest.mark.parametrize(
    "patch",
    [{"quality_escalation_reason": None}, {"quality_escalation_used": False}],
)
def test_old_trace_rejects_quality_field_injection(
    schema: str,
    patch: dict[str, object],
) -> None:
    trace = FixedFourTierV2Router(mock_seed=7).route(_request("first")).trace()
    trace["schema_version"] = schema
    trace.pop("quality_escalation_reason")
    trace.pop("quality_escalation_used")
    if schema == "fixed-four-tier-v2-mock-v2":
        trace.pop("classifier_backend")
        trace.pop("classifier_identity")
    trace.update(patch)

    with pytest.raises(ValueError):
        FixedFourTierDecision.from_trace(trace)
