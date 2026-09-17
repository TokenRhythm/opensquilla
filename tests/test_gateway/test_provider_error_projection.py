"""The three public projections share classification and a durable reference."""

from types import SimpleNamespace

import pytest

from opensquilla.gateway.rpc_sessions import _normalize_terminal_event_payload, _task_summary
from opensquilla.session.terminal_reply import build_terminal_reply, safe_error_id


@pytest.mark.parametrize(
    "value", [None, "", "ABCDEF01", "1234567", "123456789", "zzzzzzzz", 12345678]
)
def test_invalid_diagnostic_reference(value):
    assert safe_error_id(value) is None


@pytest.mark.parametrize("kind", [
    "rate_limited", "provider_overloaded", "auth_invalid", "insufficient_credits",
    "context_overflow", "model_not_found", "transport_transient", "empty_response",
    "unsupported_feature", "policy_refusal", "malformed_response", "bad_request",
])
def test_error_projection_is_safe_and_idempotent(kind):
    payload = {
        "code": "429", "message": "PRIVATE_SYNTHETIC_BODY",
        "terminal_message": "PRIVATE_SYNTHETIC_BODY",
        "error_id": "abcdef01",
        "turn_outcome": {"failure_kind": kind, "error_id": "abcdef01"},
    }
    normalized = _normalize_terminal_event_payload("session.event.error", payload)
    assert _normalize_terminal_event_payload("session.event.error", normalized) == normalized
    assert "PRIVATE" not in repr(normalized)
    assert normalized["message"].count("(ref: abcdef01)") == 1
    summary = _task_summary(SimpleNamespace(
        status="failed", error_class="429", terminal_reason="error",
        error_message=normalized["error_message"],
        details={"turn_outcome": normalized["turn_outcome"]},
    ))
    assert summary["terminal_message"] == normalized["message"]


def test_normalization_preserves_usage_replay_extensions():
    outcome = {
        "usage_call_index": 1, "no_prior_provider_dispatch": True, "replay_safe": True,
        "retry_after_ms": 100, "user_message_id": "synthetic-primary", "error_id": "abcdef01",
    }
    normalized = _normalize_terminal_event_payload("session.event.error", {
        "code": "usage_accounting_busy", "message": "usage unavailable",
        "turn_outcome": outcome, **outcome,
    })
    for key, value in outcome.items():
        assert normalized["turn_outcome"][key] == value
    assert _normalize_terminal_event_payload("session.event.error", normalized) == normalized


@pytest.mark.parametrize("other_id", ["fedcba10", "invalid", 123])
def test_conflicting_error_refs_do_not_produce_a_nested_reference(other_id):
    normalized = _normalize_terminal_event_payload("session.event.error", {
        "code": "429", "error_id": other_id,
        "turn_outcome": {"failure_kind": "rate_limited", "error_id": "abcdef01"},
    })
    assert normalized["turn_outcome"].get("error_id") is None
    assert "(ref:" not in normalized["message"]
    assert _normalize_terminal_event_payload("session.event.error", normalized) == normalized


@pytest.mark.parametrize(("code", "fragment"), [
    ("timeout", "timed out"), ("provider_output_truncated", "output limit"),
    ("provider_request_too_large", "automatic context compaction"),
    ("turn_llm_call_budget_exceeded", "budget limit"),
    ("usage_accounting_busy", "Usage accounting"),
])
def test_specific_terminal_rules_precede_provider_classification(code, fragment):
    assert fragment in build_terminal_reply({
        "status": "failed", "terminal_reason": code,
        "error_class": code, "failure_kind": "rate_limited",
    })
