from __future__ import annotations

import copy
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from opensquilla.engine.routing.fixed_four_tier_v2 import (
    INTENTS,
    TIERS,
    FixedFourTierDecision,
    FixedFourTierTaskState,
    FixedFourTierV2Router,
    RoutingRequest,
)
from opensquilla.engine.routing.jev_model import (
    JEV_ENDPOINT,
    JEV_MODEL_ID,
    JEV_PROMPT_HASH,
    JevModelClassifier,
    JevModelRuntimeError,
    build_request,
    canonical_input_hash,
)


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-key-not-a-real-credential")


def _request(**kwargs: Any) -> RoutingRequest:
    return RoutingRequest(
        session_id="session-id-never-send",
        request_id="item-id-never-send",
        message=kwargs.pop("message", "完成这项常规任务"),
        **kwargs,
    )


def _snapshot(**kwargs: Any) -> dict[str, Any]:
    return FixedFourTierV2Router._snapshot(_request(**kwargs), None, include_control_event=False)


def _response(intent: str = "new_task", tier: str = "c2") -> dict[str, Any]:
    def answer(label: str, labels: tuple[str, ...]) -> dict[str, Any]:
        return {
            "type": "choice",
            "choice": label,
            "confidence": 1.0,
            "probabilities": {key: float(key == label) for key in labels},
        }

    return {
        "model": JEV_MODEL_ID,
        "answers": {"intent": answer(intent, INTENTS), "tier": answer(tier, TIERS)},
        "usage": {"inputTokens": 100, "outputTokens": 30},
        "providerMetadata": {"gateway": {"cost": "0"}},
    }


def _classifier(
    response: dict[str, Any] | None = None,
    *,
    status: int = 200,
    events: list[dict[str, Any]] | None = None,
) -> tuple[JevModelClassifier, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if events is not None:
            assert events[-1]["event"] == "request"
        calls.append(request)
        # Allow raw invalid numeric JSON to exercise adapter validation, not
        # the mock HTTP encoder's stricter allow_nan=False default.
        return httpx.Response(status, content=json.dumps(response or _response()))

    return JevModelClassifier(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        journal=events.append if events is not None else None,
    ), calls


def test_both_heads_share_exact_canonical_input_and_one_request() -> None:
    events: list[dict[str, Any]] = []
    classifier, calls = _classifier(events=events)
    snapshot = _snapshot()
    snapshot.update({"item_id": "never-send", "quality": 0.99, "result_matrix": "secret"})
    intent = classifier.predict(snapshot)
    masked = FixedFourTierV2Router._new_task_snapshot(snapshot)
    masked["policy_allowed_tiers"] = ["c3"]
    tier = classifier.predict(masked, ("c3",))
    assert intent.label == "new_task"
    assert tier.label == "c2"
    assert set(tier.probabilities or {}) == set(TIERS)
    assert len(calls) == 1
    payload = json.loads(calls[0].content)
    assert payload == build_request(snapshot["router_input"])
    assert set(payload) == {"model", "state", "questions"}
    assert set(payload["questions"]) == {"intent", "tier"}
    assert "never-send" not in calls[0].content.decode()
    assert str(calls[0].url) == JEV_ENDPOINT
    assert calls[0].headers["authorization"] == "Bearer test-key-not-a-real-credential"
    assert [event["event"] for event in events] == ["request", "response"]
    assert events[0]["input_hash"] == canonical_input_hash(snapshot["router_input"])
    assert events[1]["status"] == 200
    assert events[1]["response"]["usage"] == {"inputTokens": 100, "outputTokens": 30}
    assert events[1]["latency_seconds"] >= 0
    assert "test-key-not-a-real-credential" not in json.dumps(events)
    assert "authorization" not in json.dumps(events).lower()
    assert classifier.identity["model_revision"] is None
    assert classifier.identity["prompt_hash"] == JEV_PROMPT_HASH


def test_cache_is_key_order_independent_but_changes_with_input() -> None:
    classifier, calls = _classifier()
    original = _snapshot()
    classifier.predict(original)
    reordered = {"router_input": dict(reversed(list(original["router_input"].items())))}
    classifier.predict(reordered, TIERS)
    assert len(calls) == 1
    classifier.predict(_snapshot(message="另一个请求"))
    assert len(calls) == 2


def test_policy_confidence_is_selected_probability_and_vendor_value_remains_in_receipt() -> None:
    body = _response()
    body["answers"]["tier"].update(
        choice="c1",
        confidence=0.71,
        probabilities={"c0": 0.21, "c1": 0.78, "c2": 0.01, "c3": 0.0},
    )
    events: list[dict[str, Any]] = []
    classifier, _ = _classifier(body, events=events)
    prediction = classifier.predict(_snapshot(), TIERS)
    assert prediction.confidence == 0.78
    assert prediction.probabilities == {"c0": 0.21, "c1": 0.78, "c2": 0.01, "c3": 0.0}
    assert events[-1]["response"]["answers"]["tier"]["confidence"] == 0.71
    router = FixedFourTierV2Router(intent_classifier=classifier, tier_classifier=classifier)
    decision, _ = router.decide(_request())
    assert decision.tier.source == "classifier"
    assert decision.tier.final == "c1"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda body: body.update(model="different-model"),
        lambda body: body["answers"].pop("intent"),
        lambda body: body["answers"]["tier"].update(type="boolean"),
        lambda body: body["answers"]["intent"].update(choice="invalid"),
        lambda body: body["answers"]["tier"]["probabilities"].pop("c3"),
        lambda body: body["answers"]["tier"]["probabilities"].update(c0=-0.1),
        lambda body: body["answers"]["tier"]["probabilities"].update(c0=True),
        lambda body: body["answers"]["tier"]["probabilities"].update(c0="0"),
        lambda body: body["answers"]["tier"]["probabilities"].update(c0=float("nan")),
        lambda body: body["answers"]["tier"]["probabilities"].update(c0=float("inf")),
        lambda body: body["answers"]["intent"].pop("confidence"),
        lambda body: body["answers"]["intent"].update(confidence=True),
        lambda body: body["answers"]["intent"].update(confidence=float("nan")),
    ],
)
def test_invalid_responses_fail_closed_after_journaling(mutator: Any) -> None:
    body = _response()
    mutator(body)
    events: list[dict[str, Any]] = []
    classifier, calls = _classifier(body, events=events)
    router = FixedFourTierV2Router(intent_classifier=classifier, tier_classifier=classifier)
    with pytest.raises(JevModelRuntimeError):
        router.decide(_request())
    assert len(calls) == 1
    assert [event["event"] for event in events] == ["request", "response"]


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_http_errors_fail_closed_without_retry(status: int) -> None:
    events: list[dict[str, Any]] = []
    classifier, calls = _classifier(status=status, events=events)
    router = FixedFourTierV2Router(intent_classifier=classifier, tier_classifier=classifier)
    with pytest.raises(JevModelRuntimeError, match=f"HTTP status {status}"):
        router.decide(_request())
    assert len(calls) == 1
    assert events[-1]["status"] == status


def test_timeout_is_in_doubt_no_retry_and_no_secret_error() -> None:
    calls = []
    events = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("test-key-not-a-real-credential", request=request)

    classifier = JevModelClassifier(
        client=httpx.Client(transport=httpx.MockTransport(handler)), journal=events.append
    )
    with pytest.raises(JevModelRuntimeError, match="in doubt") as caught:
        classifier.predict(_snapshot())
    assert len(calls) == 1
    assert [event["event"] for event in events] == ["request", "transport_error"]
    assert "test-key-not-a-real-credential" not in str(caught.value) + json.dumps(events)


def test_no_key_fails_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AI_GATEWAY_API_KEY")
    with pytest.raises(JevModelRuntimeError, match="AI_GATEWAY_API_KEY"):
        JevModelClassifier()


def test_non_json_response_is_recorded_then_rejected() -> None:
    events = []
    classifier = JevModelClassifier(
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text="not JSON test-key-not-a-real-credential")
            )
        ),
        journal=events.append,
    )
    with pytest.raises(JevModelRuntimeError, match="non-JSON"):
        classifier.predict(_snapshot())
    assert events[-1]["response"] is None
    assert events[-1]["response_text"] == "not JSON [REDACTED]"


@pytest.mark.parametrize("extra", ["benchmark_id", "item_id", "quality", "result_matrix"])
def test_forbidden_nested_evaluation_metadata_never_reaches_network(extra: str) -> None:
    classifier, calls = _classifier()
    snapshot = _snapshot(context={extra: "not allowed"})
    with pytest.raises(JevModelRuntimeError, match="forbidden"):
        classifier.predict(snapshot)
    assert not calls


def test_missing_or_malformed_canonical_input_is_rejected() -> None:
    classifier, calls = _classifier()
    for snapshot in ({}, {"router_input": {}}, _snapshot(message="")):
        with pytest.raises(JevModelRuntimeError):
            classifier.predict(snapshot)
    assert not calls


def test_first_turn_gating_and_canonical_decision_roundtrip() -> None:
    classifier, calls = _classifier(_response("continue", "c2"))
    router = FixedFourTierV2Router(intent_classifier=classifier, tier_classifier=classifier)
    request = _request(user_history=("旧历史一", "旧历史二"), previous_assistant_text="已有回答")
    decision, state = router.decide(request)
    assert decision.intent.run_status == "not_run"
    assert decision.intent.reason == "no_active_task"
    assert decision.intent.final == "new_task"
    assert decision.tier.final == state.tier == "c2"
    assert len(calls) == 1  # the joint API still computed both heads
    assert decision.classifier_backend == "jev"
    assert decision.feature_vector_status == "remote_evaluated"
    assert decision.feature_vector_dim is None
    assert decision.effective_mock_seed is None
    assert decision.feature_input_audit.input_contract == "canonical_router_input"
    input_state = json.loads(calls[0].content)["state"]
    assert input_state["history_user"] == ["旧历史一", "旧历史二"]
    assert input_state["previous_answer"] == "已有回答"
    assert decision.feature_input_audit.classifier_snapshot_hash == canonical_input_hash(
        input_state
    )
    assert FixedFourTierDecision.from_trace(decision.trace()) == decision


def test_continue_keeps_tier_and_records_gated_tier_not_run() -> None:
    classifier, calls = _classifier(_response("continue", "c3"))
    router = FixedFourTierV2Router(intent_classifier=classifier, tier_classifier=classifier)
    decision, _ = router.decide(_request(), FixedFourTierTaskState("active", "c1", 1))
    assert decision.intent.final == "continue"
    assert decision.intent.run_status == "ran"
    assert decision.tier.run_status == "not_run"
    assert decision.final_tier == "c1"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "predicted,expected,reason",
    [
        ("c0", "c2", "redo_downgrade_blocked"),
        ("c3", "c3", "classifier_argmax_selected"),
    ],
)
def test_redo_preserves_upgrade_only_policy(predicted: str, expected: str, reason: str) -> None:
    classifier, calls = _classifier(_response("redo", predicted))
    router = FixedFourTierV2Router(intent_classifier=classifier, tier_classifier=classifier)
    decision, _ = router.decide(_request(), FixedFourTierTaskState("active", "c2", 1))
    assert decision.intent.final == "redo"
    assert decision.final_tier == expected
    assert decision.tier.reason == reason
    assert len(calls) == 1


def test_tier_argmax_ignores_confidence_and_margin_thresholds() -> None:
    response = _response()
    response["answers"]["tier"].update(
        choice="c2", confidence=0.48, probabilities={"c0": 0.04, "c1": 0.04, "c2": 0.48, "c3": 0.44}
    )
    classifier, _ = _classifier(response)
    router = FixedFourTierV2Router(intent_classifier=classifier, tier_classifier=classifier)
    decision, _ = router.decide(_request())
    assert decision.final_tier == "c2"
    assert decision.tier.reason == "classifier_argmax_selected"
    assert decision.tier.confidence == 0.48


def test_tie_selects_first_maximum_in_fixed_tier_order() -> None:
    response = _response()
    response["answers"]["tier"].update(
        choice="c2", confidence=0.5, probabilities={"c0": 0, "c1": 0, "c2": 0.5, "c3": 0.5}
    )
    classifier, _ = _classifier(response)
    router = FixedFourTierV2Router(intent_classifier=classifier, tier_classifier=classifier)
    decision, _ = router.decide(_request())
    assert decision.final_tier == "c2"
    assert decision.tier.reason == "classifier_argmax_selected"


def test_non_argmax_tier_ignores_raw_choice_without_rewriting_receipt() -> None:
    body = _response()
    body["answers"]["tier"].update(
        choice="c3",
        confidence=0.32,
        probabilities={"c0": 0.01, "c1": 0.02, "c2": 0.49, "c3": 0.48},
    )
    events: list[dict[str, Any]] = []
    classifier, calls = _classifier(body, events=events)
    prediction = classifier.predict(_snapshot(), TIERS)
    assert prediction.label == "c2"
    assert prediction.confidence == 0.49
    router = FixedFourTierV2Router(intent_classifier=classifier, tier_classifier=classifier)
    decision, _ = router.decide(_request())
    assert len(calls) == 1
    assert decision.final_tier == "c2"
    assert decision.tier.source == "classifier"
    assert decision.tier.run_status == "ran"
    assert decision.tier.reason == "classifier_argmax_selected"
    assert events[-1]["response"]["answers"]["tier"] == body["answers"]["tier"]
    assert FixedFourTierDecision.from_trace(decision.trace()) == decision


@pytest.mark.parametrize("fields", [
    {},
    {"choice": "invalid", "confidence": None},
    {"choice": "c0", "confidence": 0.0},
    {"choice": "c0", "confidence": True},
    {"choice": "c0", "confidence": float("nan")},
])
def test_tier_choice_and_confidence_are_not_required_or_used(fields: dict[str, Any]) -> None:
    body = _response()
    body["answers"]["tier"] = {
        "type": "choice",
        "probabilities": {"c3": 0.30, "c2": 0.29, "c1": 0.21, "c0": 0.20},
        **fields,
    }
    classifier, calls = _classifier(body)
    router = FixedFourTierV2Router(
        intent_classifier=classifier, tier_classifier=classifier,
        tier_min_confidence=0.99, min_margin=0.99,
    )
    decision, _ = router.decide(_request())
    assert decision.final_tier == "c3"
    assert decision.tier.source == "classifier"
    assert decision.tier.confidence == 0.30
    assert decision.classifier_identity["tier_selection_mode"] == "probability_argmax.v1"
    assert len(calls) == 1
    assert FixedFourTierDecision.from_trace(decision.trace()) == decision


@pytest.mark.parametrize("probabilities, expected", [
    ({"c3": 0.5, "c2": 0.5, "c1": 0, "c0": 0}, "c2"),
    ({"c3": 0.25, "c2": 0.25, "c1": 0.25, "c0": 0.25}, "c0"),
    ({"c3": 0, "c2": 0, "c1": 0, "c0": 0}, "c0"),
])
def test_tier_argmax_ties_ignore_json_key_order(probabilities, expected) -> None:
    body = _response()
    body["answers"]["tier"]["probabilities"] = probabilities
    classifier, _ = _classifier(body)
    router = FixedFourTierV2Router(intent_classifier=classifier, tier_classifier=classifier)
    decision, _ = router.decide(_request())
    assert decision.final_tier == expected
    assert decision.tier.source == "classifier"
    assert decision.tier.reason == "classifier_argmax_selected"


def test_argmax_trace_cannot_relabel_a_legacy_uncertainty_fallback() -> None:
    classifier, _ = _classifier()
    router = FixedFourTierV2Router(intent_classifier=classifier, tier_classifier=classifier)
    decision, _ = router.decide(_request())
    trace = decision.trace()
    trace["tier"].update(source="fallback", reason="classifier_uncertain")
    with pytest.raises(ValueError, match="cannot use confidence"):
        FixedFourTierDecision.from_trace(trace)


def test_non_argmax_intent_uses_original_active_task_continue_fallback() -> None:
    body = _response(intent="redo", tier="c3")
    body["answers"]["intent"].update(
        choice="redo",
        confidence=0.30,
        probabilities={"continue": 0.10, "redo": 0.40, "new_task": 0.50},
    )
    events: list[dict[str, Any]] = []
    classifier, calls = _classifier(body, events=events)
    router = FixedFourTierV2Router(intent_classifier=classifier, tier_classifier=classifier)
    decision, _ = router.decide(_request(), FixedFourTierTaskState("active", "c2", 1))
    assert len(calls) == 1
    assert decision.intent.final == "continue"
    assert decision.intent.source == "fallback"
    assert decision.intent.reason == "invalid_classifier_result"
    assert decision.final_tier == "c2"
    assert decision.tier.run_status == "not_run"
    assert events[-1]["response"]["answers"]["intent"] == body["answers"]["intent"]
    assert events[-1]["response"]["answers"]["tier"]["choice"] == "c3"


def test_non_normalized_probability_sum_is_preserved_and_tier_uses_argmax() -> None:
    body = _response()
    raw_probabilities = {"c0": 0.02, "c1": 0.07, "c2": 0.8, "c3": 0.1}
    body["answers"]["tier"].update(
        choice="c2",
        confidence=0.72,
        probabilities=raw_probabilities,
    )
    events: list[dict[str, Any]] = []
    classifier, calls = _classifier(body, events=events)
    prediction = classifier.predict(_snapshot(), TIERS)
    assert prediction.label == "c2"
    assert prediction.confidence == 0.8
    assert prediction.probabilities == raw_probabilities
    assert sum(prediction.probabilities.values()) == sum(raw_probabilities.values())
    router = FixedFourTierV2Router(intent_classifier=classifier, tier_classifier=classifier)
    decision, _ = router.decide(_request())
    assert len(calls) == 1
    assert decision.final_tier == "c2"
    assert decision.tier.source == "classifier"
    assert decision.tier.run_status == "ran"
    assert decision.tier.reason == "classifier_argmax_selected"
    assert events[-1]["response"]["answers"]["tier"] == body["answers"]["tier"]
    assert FixedFourTierDecision.from_trace(decision.trace()) == decision


def test_invalid_intent_probability_sum_uses_original_active_task_continue_fallback() -> None:
    body = _response(intent="redo", tier="c3")
    body["answers"]["intent"].update(
        choice="redo",
        confidence=0.70,
        probabilities={"continue": 0.1, "redo": 0.8, "new_task": 0.09},
    )
    events: list[dict[str, Any]] = []
    classifier, calls = _classifier(body, events=events)
    router = FixedFourTierV2Router(intent_classifier=classifier, tier_classifier=classifier)
    decision, _ = router.decide(_request(), FixedFourTierTaskState("active", "c2", 1))
    assert len(calls) == 1
    assert decision.intent.final == "continue"
    assert decision.intent.source == "fallback"
    assert decision.intent.reason == "invalid_classifier_result"
    assert decision.final_tier == "c2"
    assert decision.tier.run_status == "not_run"
    assert events[-1]["response"]["answers"]["intent"] == body["answers"]["intent"]
    assert events[-1]["response"]["answers"]["tier"]["choice"] == "c3"


def test_seeded_receipt_avoids_network_and_closed_classifier_is_rejected() -> None:
    classifier, calls = _classifier()
    snapshot = _snapshot()
    classifier.load_cached_response(snapshot["router_input"], _response("redo", "c3"))
    assert classifier.predict(snapshot).label == "redo"
    assert classifier.predict(snapshot, TIERS).label == "c3"
    assert not calls
    classifier.close()
    with pytest.raises(JevModelRuntimeError, match="closed"):
        classifier.predict(snapshot)


def test_journal_failure_stops_before_sending_request() -> None:
    calls = []

    def journal(value: dict[str, Any]) -> None:
        raise OSError("secret internal disk path")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_response())

    classifier = JevModelClassifier(
        client=httpx.Client(transport=httpx.MockTransport(handler)), journal=journal
    )
    with pytest.raises(JevModelRuntimeError, match="journal persistence"):
        classifier.predict(_snapshot())
    assert not calls


def test_decision_rejects_fake_native_identity_and_mock_seed() -> None:
    classifier, _ = _classifier()
    decision, _ = FixedFourTierV2Router(
        intent_classifier=classifier, tier_classifier=classifier
    ).decide(_request())
    with pytest.raises(ValueError, match="mock seed"):
        replace(decision, effective_mock_seed=123)
    with pytest.raises(ValueError, match="remote feature"):
        replace(decision, feature_vector_status="materialized", feature_vector_dim=380)
    identity = copy.deepcopy(dict(decision.classifier_identity or {}))
    identity["execution_mode"] = "native_embedded"
    with pytest.raises(ValueError, match="remote identity"):
        replace(decision, classifier_identity=identity)
