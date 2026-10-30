"""Replay fixtures generated from the frozen experiment's actual state machine.

Oracle: runtime-source-manifest.json (2026-10-20), source revision provenance
bf1306f8c41b3362952d4fc9469fb1567e1033da plus its frozen working diff.
The expected rows below were executed with that export's FixedFourTierV2Router
and apply_four_tier_squilla_control, not derived from this implementation.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine.routing.model_policy import (
    ModelPolicyDecision,
    ModelPolicyState,
    decide_model_tier,
    quality_retry_tier,
)


def cases() -> list[dict[str, Any]]:
    rows = []
    for previous in (None, "c0", "c1", "c2", "c3"):
        for intent in ("continue", "redo", "new_task"):
            rows.append({"previous": previous, "intent": intent, "predicted": "c0"})
    for tier in ("c0", "c1", "c2", "c3"):
        for text in (
            "hello",
            "谢谢！",
            "deploy to production",
            "review the architecture",
            "return JSON",
            "Traceback (most recent call last): exception",
            "a " * 3_001,
            "```" + "x" * 1_500 + "```",
            "src/one.py src/two.py",
            "INFO x\n" * 300,
        ):
            rows.append({"predicted": tier, "message": text})
    for intent in ("continue", "redo", "new_task"):
        rows.append({"previous": "c0", "intent": intent, "held": "c3"})
        rows.append(
            {
                "previous": "c1",
                "intent": intent,
                "message": "Your previous answer is wrong.",
                "context": {
                    "context_tokens_est": 90000,
                    "material_estimated_tokens": 90000,
                    "turn_index": 8,
                    "tier_capabilities": {"c0": {"context_window": 50}},
                    "budget": {"action": "cap", "limit_usd": 1, "spend_usd": 2, "cap_tier": "c0"},
                },
            }
        )
    for event in ("new_task", "new", "redo", "regenerate"):
        for previous in (None, "c1"):
            rows.append({"previous": previous, "event": event})
    for text in ("/new", "/redo", "start a new task: explain", "重新回答一次"):
        rows.append({"previous": "c1", "message": text})
    rows.extend(
        [
            {
                "previous": "c1",
                "intent_probs": {"new_task": 0.3333334, "continue": 0.3333333, "redo": 0.3333333},
                "tier_probs": {"c0": 0.26, "c1": 0.25, "c2": 0.25, "c3": 0.24},
            },
            {"previous": "c1", "intent_probs": {"new_task": 0.5, "continue": 0.5, "redo": 0}},
            {"previous": "c1", "intent_probs": {"new_task": 0.5, "continue": 0, "redo": 0.5}},
            {"tier_probs": {"c0": 0.25, "c1": 0.25, "c2": 0.25, "c3": 0.25}},
            {"previous": "c2", "failure": True},
            {"failure": True},
            {"previous": "c2", "intent_probs": {"continue": 1}},
            {"previous": "c2", "intent": "new_task", "tier_probs": {"c0": 1}},
            {
                "previous": "c1",
                "context": {"flags_text_override": "hello", "context_tokens_est": 2000},
                "message": "production",
            },
            {"previous": "c2", "context": {"context_tokens_est": 2001}},
        ]
    )
    return rows


def fixture(row: dict[str, Any]):
    prior = row.get("previous")
    state = (
        ModelPolicyState(prior, "old-task", 2, 7, "old-start", "generation-start")
        if prior
        else None
    )
    router_input = {
        "current_request": row.get("message", "Please continue."),
        "task_anchor": "Original task",
        "history_user": ["older task", "one", "two", "three", "four"],
        "previous_answer": "Previous response",
        "previous_usage": {},
        "previous_outcome": "success",
        "active_route_tier": prior.upper() if prior else None,
        "route_history": [],
        "context": {},
        "tool_state": {"available_tools": ["exec"]},
        "attachments": [],
    }
    intent = row.get("intent", "continue")
    tier = row.get("predicted", "c0")
    result = {
        "intent_probabilities": row.get(
            "intent_probs",
            {label: float(label == intent) for label in ("new_task", "continue", "redo")},
        ),
        "tier_probabilities": row.get(
            "tier_probs", {label: float(label == tier) for label in ("c0", "c1", "c2", "c3")}
        ),
    }
    return state, router_input, result


class Classifier:
    def __init__(self, result: Any, *, failure: bool = False):
        self.result = result
        self.failure = failure
        self.inputs = []

    def predict(self, router_input):
        self.inputs.append(copy.deepcopy(router_input))
        if self.failure:
            raise RuntimeError("test inference failed")
        return self.result


def project(decision):
    return [
        decision.intent,
        decision.tier,
        decision.raw_tier,
        decision.thinking_mode,
        decision.prompt_policy,
        decision.context_reset,
        decision.intent_audit.to_dict(),
        decision.tier_audit.to_dict(),
        decision.next_state.turn_count,
        decision.next_state.generation_start_input_message_id,
        decision.metadata["flags"],
    ]


# FROZEN_EXPECTED
FROZEN_EXPECTED = json.loads(
    (Path(__file__).parent / "goldens" / "model_tier_policy_reference.json").read_text()
)


@pytest.mark.parametrize("row,expected", list(zip(cases(), FROZEN_EXPECTED, strict=True)))
def test_frozen_cross_project_parity(row, expected):
    state, router_input, result = fixture(row)
    classifier = Classifier(result, failure=row.get("failure", False))
    decision = decide_model_tier(
        router_input,
        state,
        classifier,
        explicit_control=row.get("event"),
        held_tier=row.get("held"),
        input_message_id="current-input",
        control_context=row.get("context", {}),
        task_id_factory=lambda: "new-task",
    )
    assert project(decision) == expected
    assert ModelPolicyDecision.from_dict(decision.to_dict()) == decision
    assert ModelPolicyState.from_dict(decision.next_state.to_dict()) == decision.next_state
    assert len(classifier.inputs) <= 1


def test_raw_input_no_mutation_or_legacy_text_truncation():
    state, router_input, result = fixture({"previous": "c1", "intent": "new_task"})
    router_input["current_request"] = "x" * 30_000
    router_input["previous_answer"] = "y" * 30_000
    original = copy.deepcopy(router_input)
    classifier = Classifier(result)
    decision = decide_model_tier(router_input, state, classifier, input_message_id="now")
    assert router_input == original
    assert classifier.inputs[0]["current_request"] == original["current_request"]
    assert classifier.inputs[0]["previous_answer"] == original["previous_answer"]
    assert classifier.inputs[0]["history_user"] == ["one", "two", "three", "four"]
    assert decision.context_reset is False
    assert decision.next_state.task_start_input_message_id == "now"
    assert decision.next_state.generation_start_input_message_id == "generation-start"


def test_integrity_failure_is_not_swallowed_as_classifier_fallback():
    class IntegrityError(RuntimeError):
        fail_closed = True

    class Broken:
        def predict(self, value):
            raise IntegrityError("manifest changed")

    with pytest.raises(IntegrityError):
        decide_model_tier({"current_request": "hello"}, None, Broken())


@pytest.mark.parametrize("reason", ["validation_failure", "no_progress"])
@pytest.mark.parametrize(
    "previous,next_tier", [("c0", "c1"), ("c1", "c2"), ("c2", "c3"), ("c3", None)]
)
def test_bounded_concrete_failure_recovery(reason, previous, next_tier):
    assert quality_retry_tier(previous, reason, 1) == next_tier
    assert quality_retry_tier(previous, reason, 0) is None
    assert quality_retry_tier(previous, reason, 1, already_used=True) is None
    assert quality_retry_tier(previous, reason, 1, held_tier="c0") is None


@pytest.mark.parametrize(
    "reason",
    ["explicit_correction", "timeout", "rate_limit", "connection_error", "authentication_error"],
)
def test_correction_and_transport_errors_cannot_upgrade(reason):
    assert quality_retry_tier("c0", reason, 2) is None


def test_replay_rejects_tampering_and_incompatible_state():
    state, router_input, result = fixture({"previous": "c1"})
    decision = decide_model_tier(router_input, state, Classifier(result))
    payload = decision.to_dict()
    payload["tier"] = "c3"
    with pytest.raises(ValueError, match="checksum"):
        ModelPolicyDecision.from_dict(payload)
    payload = state.to_dict()
    payload["policy_version"] = "future"
    with pytest.raises(ValueError, match="incompatible"):
        ModelPolicyState.from_dict(payload)


def test_frozen_controller_rule_identity():
    state, router_input, result = fixture({})
    decision = decide_model_tier(router_input, state, Classifier(result))
    assert decision.metadata["runtime_rules_sha256"] == (
        "2155db23882e40b5cef486a9f88b37b0c5f5e2c126c0cc27824f7e812575816d"
    )


@pytest.mark.parametrize(
    "mutation", ["jump", "fabricated_hold", "skipped_probabilities", "correction_guard"]
)
def test_replay_rejects_rehashed_semantic_tampering(mutation):
    import hashlib

    state, router_input, result = fixture({"previous": "c0", "intent": "redo"})
    payload = decide_model_tier(router_input, state, Classifier(result)).to_dict()
    if mutation == "jump":
        payload["tier"] = payload["raw_tier"] = payload["next_state"]["tier"] = "c2"
        payload["tier_audit"]["final"] = "c2"
    elif mutation == "fabricated_hold":
        payload["metadata"]["router_control_hold_applied"] = True
    elif mutation == "skipped_probabilities":
        payload["tier_audit"]["probabilities"] = {"c0": 0, "c1": 1, "c2": 0, "c3": 0}
    else:
        payload["metadata"].pop("quality_escalation_satisfied_by_prior_policy")
    payload.pop("trace_hash")
    payload["trace_hash"] = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    with pytest.raises(ValueError):
        ModelPolicyDecision.from_dict(payload)
