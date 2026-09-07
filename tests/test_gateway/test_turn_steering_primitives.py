from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.application.turn_admission import PendingInputGuard
from opensquilla.application.turn_steering import (
    PreparedSteeringInput,
    SteeringAcceptanceError,
    SteeringIdentity,
    SteeringRollbackError,
)
from opensquilla.gateway.rpc import RpcHandlerError
from opensquilla.gateway.turn_steering import (
    GatewaySteeringPrimitives,
    decode_steering_command,
    map_steering_error,
)
from opensquilla.session.models import TurnIngressReceipt
from opensquilla.session.storage import (
    PendingChatInputConflictError,
    PendingChatInputNotFoundError,
    StaleEpochError,
    StorageBusyError,
    TurnAcceptanceResult,
    TurnIngressConflictError,
)


def _params() -> dict[str, object]:
    return {
        "key": "agent:main:webchat:steering-wire",
        "message": "change direction",
        "expected_turn_id": "turn-one",
        "client_request_id": "request-snake",
        "client_message_id": "client-snake",
    }


def test_steering_decoder_preserves_request_id_alias_priority_and_source_authority() -> None:
    command = decode_steering_command(
        {
            **_params(),
            "clientRequestId": "request-camel",
            "clientMessageId": "client-camel",
            "_source": {"callerKind": "cli", "channelKind": "cli", "role": "owner"},
        },
        key="agent:main:webchat:steering-wire",
        durable=True,
        principal_role="operator",
        connection_id="connection-one",
    )
    assert command.client_request_id == "request-camel"
    assert command.client_message_id == "client-snake"
    assert command.source_scope == "cli:cli:operator:steer.v2"
    assert command.surface_id == "cli:cli"
    assert command.is_web_source is False
    assert not hasattr(command, "attributes")


def test_steering_decoder_does_not_fall_through_a_present_null_identity_alias() -> None:
    with pytest.raises(ValueError, match="expected_turn_id is required"):
        decode_steering_command(
            {**_params(), "expected_turn_id": None, "expectedTurnId": "other-turn"},
            key="agent:main:webchat:steering-wire",
            durable=True,
            principal_role="operator",
            connection_id="connection-one",
        )


def test_pending_steering_keeps_staged_identity_and_legacy_surface_default() -> None:
    guard = PendingInputGuard("pending-one", "staged-fingerprint", 2, "cli:cli:operator")
    command = decode_steering_command(
        _params(),
        key="agent:main:webchat:steering-wire",
        durable=True,
        principal_role="operator",
        connection_id="connection-one",
        pending=guard,
    )
    assert command.pending_input == guard
    assert command.request_fingerprint == ""
    legacy = decode_steering_command(
        {"message": "guide"},
        key="agent:main:webchat:steering-wire",
        durable=False,
        principal_role="operator",
        connection_id="connection-one",
    )
    assert legacy.surface_id == "web:connection-one"
    assert legacy.client_message_id


@pytest.mark.parametrize(
    ("failure", "code", "fallback_safe", "retryable"),
    [
        (StaleEpochError(), "SESSION_CHANGED", True, True),
        (TurnIngressConflictError("conflict"), "IDEMPOTENCY_CONFLICT", False, False),
        (PendingChatInputNotFoundError(), "PENDING_INPUT_NOT_FOUND", False, True),
        (PendingChatInputConflictError(), "PENDING_INPUT_CONFLICT", False, True),
        (
            StorageBusyError("accept_turn", waited_ms=20, retry_after_ms=50),
            "STORAGE_BUSY",
            False,
            True,
        ),
    ],
)
def test_atomic_steering_failure_retains_v4_retry_safety(
    failure, code, fallback_safe, retryable
) -> None:
    mapped = map_steering_error(SteeringAcceptanceError(failure))
    assert isinstance(mapped, RpcHandlerError)
    assert mapped.code == code
    assert mapped.accepted is False
    assert mapped.retryable is retryable
    assert mapped.details["fallback_safe"] is fallback_safe
    if isinstance(failure, StorageBusyError):
        assert mapped.details == {
            "operation": "accept_turn",
            "waited_ms": 20,
            "fallback_safe": False,
        }
        assert mapped.retry_after_ms == 50


def test_read_phase_storage_failure_is_not_reclassified_as_an_acceptance_attempt() -> None:
    error = StorageBusyError("receipt", waited_ms=20, retry_after_ms=50)
    assert map_steering_error(error) is error


@pytest.mark.parametrize(
    "failure",
    [
        StorageBusyError("accept_turn", waited_ms=20, retry_after_ms=50),
        StaleEpochError(),
        TurnIngressConflictError("conflict"),
        PendingChatInputNotFoundError(),
        PendingChatInputConflictError(),
    ],
)
def test_primitive_classifies_only_known_native_acceptance_failures(failure) -> None:
    projected = GatewaySteeringPrimitives.acceptance_failure(failure)
    assert isinstance(projected, SteeringAcceptanceError)
    assert projected.failure is failure
    assert GatewaySteeringPrimitives.acceptance_failure(RuntimeError("unrelated")) is None


def _native_ports(storage):
    return GatewaySteeringPrimitives(
        session_manager=SimpleNamespace(storage=storage),
        task_runtime=None,
        turn_runner=None,
        emit_steer=AsyncMock(),
        emit_disposition=AsyncMock(),
    )


async def test_receipt_view_retains_native_identity_and_acceptance_timestamp() -> None:
    identity = SteeringIdentity("web:web:operator", "session-key", "request", "fingerprint")
    acceptance = TurnAcceptanceResult(
        TurnIngressReceipt(
            source_scope=identity.source_scope,
            request_session_key=identity.request_session_key,
            client_request_id=identity.client_request_id,
            request_fingerprint=identity.request_fingerprint,
            accepted_session_key="session-key",
            session_id="session-id",
            message_id="message-id",
            task_id="task-id",
            accepted_at=123,
        ),
        replayed=True,
        fresh_user_session=False,
    )
    storage = SimpleNamespace(get_turn_ingress_receipt=AsyncMock(return_value=acceptance))
    result = await _native_ports(storage).receipt(identity)
    assert result is acceptance
    assert result.receipt is acceptance.receipt
    assert result.receipt.accepted_at == 123


async def test_invalid_native_transcript_is_rejected_before_persistence() -> None:
    storage = SimpleNamespace(accept_turn=AsyncMock())
    prepared = PreparedSteeringInput(
        SimpleNamespace(
            content="hello", message_id="message-id", session_id="session-id", session_key="key"
        ),
        7,
    )
    with pytest.raises(TypeError, match="native transcript entry"):
        await _native_ports(storage).persist(
            prepared,
            active_turn_id="turn-id",
            identity=SteeringIdentity("source", "key", "request", "fingerprint"),
            workspace_guard=None,
            pending=None,
        )
    storage.accept_turn.assert_not_awaited()


def test_dirty_legacy_error_preserves_orphan_identity_and_disables_fallback() -> None:
    mapped = map_steering_error(SteeringRollbackError("key-one", "orphan-one", "target-one"))
    assert isinstance(mapped, RpcHandlerError)
    assert mapped.code == "STEER_RACE_DIRTY"
    assert mapped.retryable is False
    assert mapped.details == {
        "session_key": "key-one",
        "orphan_message_id": "orphan-one",
        "target_turn_id": "target-one",
        "fallback_safe": False,
        "remediation": "dedup by orphan_message_id before resending",
    }
