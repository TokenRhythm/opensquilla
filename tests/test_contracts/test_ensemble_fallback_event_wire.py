"""Typed event contracts for the ensemble fallback repair boundary."""

from __future__ import annotations

import json
from typing import get_args

import pytest
from pydantic import ValidationError

from opensquilla.contracts.turn_execution import StickyExecutionRole
from opensquilla.engine.types import (
    AgentEvent,
    AnswerGenerationResetEvent,
    ControlTerminalEvent,
    ControlTerminalReason,
)
from opensquilla.gateway.protocol import (
    AnswerGenerationResetWire,
    ControlTerminalWire,
    ProviderAttemptFailureWire,
    deserialize_public_event,
    is_public_event,
    serialize_provider_attempt_failure,
    serialize_public_event,
)
from opensquilla.provider.failures import ProviderFailureKind
from opensquilla.provider.types import ProviderAttemptFailure, StreamEvent


def test_additive_defaults_keep_all_contracts_constructible() -> None:
    failure = ProviderAttemptFailure()
    reset = AnswerGenerationResetEvent()
    control = ControlTerminalEvent()

    assert failure.kind == "provider_attempt_failure"
    assert failure.request_started is False
    assert failure.retryable is False
    assert failure.safe_message == ""
    assert reset.kind == "answer_generation_reset"
    assert reset.preserve_completed_tools is True
    assert reset.terminal is False
    assert control.kind == "control_terminal"
    assert control.reason is ControlTerminalReason.CANCEL
    assert control.preserve_completed_tools is True
    assert control.terminal is True

    assert ProviderAttemptFailureWire().model_dump(mode="json")["kind"] == (
        "provider_attempt_failure"
    )
    assert AnswerGenerationResetWire().model_dump(mode="json")["kind"] == (
        "answer_generation_reset"
    )
    assert ControlTerminalWire().model_dump(mode="json")["kind"] == "control_terminal"


def test_provider_attempt_failure_round_trips_only_safe_internal_fields() -> None:
    failure = ProviderAttemptFailure(
        turn_id="turn-1",
        assistant_message_id="assistant-1",
        role=StickyExecutionRole.PRIMARY_AGGREGATOR,
        logical_call_index=2,
        attempt_index=1,
        lease_id="lease-1",
        provider="tokenrhythm",
        model="model-a",
        failure_kind=ProviderFailureKind.TRANSPORT_TRANSIENT,
        retryable=True,
        request_started=True,
        safe_message="Authorization: Bearer must-not-leak",
        generation_epoch=3,
        sequence=11,
    )

    payload = serialize_provider_attempt_failure(failure)
    restored = ProviderAttemptFailureWire.model_validate_json(json.dumps(payload))

    assert restored.model_dump(mode="json") == payload
    assert payload["failure_kind"] == "transport_transient"
    assert payload["safe_message"] == "Authorization: Bearer ***"
    assert "must-not-leak" not in json.dumps(payload)
    assert "raw_provider_payload" not in ProviderAttemptFailureWire.model_validate(
        {**payload, "raw_provider_payload": {"secret": "do-not-copy"}}
    ).model_dump(mode="json")


def test_generation_reset_round_trips_authoritative_same_message_snapshots() -> None:
    event = AnswerGenerationResetEvent(
        turn_id="turn-1",
        assistant_message_id="assistant-1",
        old_generation_epoch=0,
        new_generation_epoch=1,
        from_role=StickyExecutionRole.PRIMARY_AGGREGATOR,
        to_role=StickyExecutionRole.FIXED_AGGREGATOR,
        safe_reason="primary_attempt_failed",
        preserve_completed_tools=True,
        authoritative_text_snapshot="",
        authoritative_reasoning_snapshot="",
        sequence=17,
        terminal=False,
        terminal_error_message="internal safe failure",
        terminal_error_code="provider_error",
        terminal_failure_kind="unknown",
    )

    payload = serialize_public_event(event)
    restored = deserialize_public_event(json.loads(json.dumps(payload)))

    assert isinstance(restored, AnswerGenerationResetWire)
    assert restored.model_dump(mode="json") == payload
    assert payload["assistant_message_id"] == "assistant-1"
    assert payload["old_generation_epoch"] == 0
    assert payload["new_generation_epoch"] == 1
    assert payload["preserve_completed_tools"] is True
    assert payload["terminal_text_snapshot"] is None
    assert "terminal_error_message" not in payload
    assert "terminal_error_code" not in payload
    assert "terminal_failure_kind" not in payload


@pytest.mark.parametrize("reason", list(ControlTerminalReason))
def test_control_terminal_reason_enum_and_round_trip(reason: ControlTerminalReason) -> None:
    event = ControlTerminalEvent(
        turn_id="turn-1",
        assistant_message_id="assistant-1",
        sequence=21,
        reason=reason,
    )

    payload = serialize_public_event(event)
    restored = ControlTerminalWire.model_validate_json(json.dumps(payload))

    assert set(reason.value for reason in ControlTerminalReason) == {
        "cancel",
        "shutdown",
        "hard_deadline",
        "platform_validation",
        "platform_safety",
    }
    assert payload["reason"] == reason.value
    assert restored.reason is reason
    assert restored.preserve_completed_tools is True
    assert restored.terminal is True


def test_provider_failure_is_not_a_public_event_by_default() -> None:
    failure = ProviderAttemptFailure(
        failure_kind=ProviderFailureKind.AUTH_INVALID,
        safe_message="provider rejected credentials",
    )
    reset = AnswerGenerationResetEvent(
        assistant_message_id="assistant-1",
        old_generation_epoch=0,
        new_generation_epoch=1,
        safe_reason="retry",
    )
    control = ControlTerminalEvent(
        assistant_message_id="assistant-1",
        reason=ControlTerminalReason.SHUTDOWN,
    )

    assert ProviderAttemptFailure not in get_args(StreamEvent)
    assert ProviderAttemptFailure not in get_args(AgentEvent)
    assert not is_public_event(failure)
    assert is_public_event(reset)
    assert is_public_event(control)

    with pytest.raises(TypeError, match="not public"):
        serialize_public_event(failure)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        deserialize_public_event(
            {
                "kind": "provider_attempt_failure",
                "failure_kind": "auth_invalid",
                "safe_message": "internal only",
            }
        )
