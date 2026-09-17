from __future__ import annotations

import pytest

from opensquilla.engine.context_budget import coordinate_provider_context_budget
from opensquilla.provider.request_proof import RESPONSES_REQUEST_ENVELOPE


def test_context_budget_sends_payload_when_proof_is_disabled() -> None:
    payload = {"messages": [{"role": "user", "content": "hello"}]}

    decision = coordinate_provider_context_budget(
        payload,
        projection_adapter="test",
        proof_budget=0,
    )

    assert decision.action == "send"
    assert decision.payload == payload
    assert decision.proof is None


def test_context_budget_reuses_provider_proof_for_budget_limited() -> None:
    payload = {"messages": [{"role": "user", "content": "x" * 5000}]}

    decision = coordinate_provider_context_budget(
        payload,
        projection_adapter="test",
        proof_budget=100,
    )

    assert decision.action == "budget_limited"
    assert decision.payload is None
    assert decision.reason == "provider_request_budget_exhausted"
    assert decision.proof is not None
    assert decision.proof["fallback_reason"] == "provider_request_budget_exhausted"


def test_context_budget_reports_send_compacted_when_provider_proof_compacts() -> None:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": "Update the file.",
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": "x" * 5000,
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "File updated.",
            },
            {
                "role": "assistant",
                "content": "The file was updated successfully.",
            },
        ]
    }

    decision = coordinate_provider_context_budget(
        payload,
        projection_adapter="test",
        proof_budget=2000,
    )

    assert decision.action == "send_compacted"
    assert decision.proof is not None
    assert decision.proof["compact_needed"] is True
    assert decision.payload is not None


def test_context_budget_does_not_compact_unresolved_tool_call() -> None:
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_unresolved",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": "x" * 5000,
                        },
                    }
                ],
            }
        ]
    }

    decision = coordinate_provider_context_budget(
        payload,
        projection_adapter="test",
        proof_budget=2000,
    )

    assert decision.action == "budget_limited"
    assert decision.payload is None
    assert decision.reason == "provider_request_budget_exhausted"
    assert decision.proof is not None


def test_context_budget_accounts_for_responses_input_and_instructions() -> None:
    payload = {
        "model": "synthetic-model",
        "input": [{"role": "user", "content": "x" * 5000}],
        "instructions": "trusted system",
        "tools": [{"type": "function", "name": "lookup"}],
        "store": False,
    }

    decision = coordinate_provider_context_budget(
        payload,
        projection_adapter="synthetic_responses",
        proof_budget=1000,
        envelope_shape=RESPONSES_REQUEST_ENVELOPE,
    )

    assert decision.action == "budget_limited"
    assert decision.payload is None
    assert decision.proof is not None
    assert decision.proof["request_sequence_key"] == "input"
    assert decision.proof["request_system_key"] == "instructions"
    assert decision.proof["request_compaction_supported"] is False
    assert decision.proof["conversation_chars"] > 5000
    assert decision.proof["messages_chars"] == decision.proof["conversation_chars"]
    assert decision.proof["system_chars"] > 0
    assert decision.proof["tools_chars"] > 0
    assert decision.proof["top_level_chars"] > 0
    assert decision.proof["retry_count"] == 0
    assert decision.proof["usage_source"] == "projected_text_envelope_tokens"
    assert decision.proof["usage_confidence"] in {
        "tokenizer_estimate",
        "conservative_estimate",
    }
    assert decision.proof["wire_json_chars"] >= decision.proof["projected_context_chars"]


def test_context_budget_returns_controlled_invalid_request_for_non_json_payload() -> None:
    decision = coordinate_provider_context_budget(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "tool_choice": {"invalid"},
        },
        projection_adapter="synthetic",
        proof_budget=10_000,
    )

    assert decision.action == "invalid_request"
    assert decision.payload is None
    assert decision.proof is None
    assert decision.reason == "provider_request_serialization_failed"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("proof_budget", [0, 10_000])
def test_context_budget_rejects_non_finite_json_numbers(
    value: float,
    proof_budget: int,
) -> None:
    decision = coordinate_provider_context_budget(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": value,
        },
        projection_adapter="synthetic",
        proof_budget=proof_budget,
    )

    assert decision.action == "invalid_request"
    assert decision.payload is None
    assert decision.proof is None
    assert decision.reason == "provider_request_serialization_failed"
