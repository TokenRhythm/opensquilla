from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from opensquilla.application.admission_errors import AdmissionError, AdmissionUnavailableError
from opensquilla.application.turn_admission import (
    AdmitTurn,
    CancelTurn,
    SteerTurn,
    TurnAdmission,
)
from opensquilla.gateway.adapters.turn_admission import (
    GatewayTurnAdmissionAdapter,
    webchat_session_key,
)
from opensquilla.gateway.rpc import RpcHandlerError, RpcUnavailableError


def test_webchat_session_key_preserves_agent_scope() -> None:
    assert webchat_session_key("agent:kid-project:webchat:abc") == "agent:kid-project:webchat:abc"


class _Ports:
    def __init__(self) -> None:
        self.admit_call = AsyncMock(return_value={"status": "accepted", "key": "canonical"})
        self.cancel_call = AsyncMock(return_value={"aborted": True, "key": "canonical"})
        self.steer_call = AsyncMock(return_value={"accepted": True, "key": "canonical"})

    async def admit(self, command: AdmitTurn) -> dict[str, Any]:
        return await self.admit_call(command)

    async def cancel(self, command: CancelTurn) -> dict[str, Any]:
        return await self.cancel_call(command)

    async def steer(self, command: SteerTurn) -> dict[str, Any]:
        return await self.steer_call(command)


def _adapter() -> tuple[
    GatewayTurnAdmissionAdapter,
    _Ports,
]:
    ports = _Ports()
    adapter = GatewayTurnAdmissionAdapter(
        TurnAdmission(
            ingress=ports,
            cancellation=ports,
            steering=ports,
        )
    )
    return adapter, ports


async def test_adapter_projects_semantic_commands_to_existing_runtime() -> None:
    adapter, ports = _adapter()

    await adapter.admit(
        {"key": "canonical", "message": "hello", "intent": "continue"},
        surface="session",
    )
    await adapter.cancel(
        {
            "key": "canonical",
            "taskId": "task-1",
            "scope": "task",
            "source": "test",
        },
        surface="session",
    )
    await adapter.steer(
        {
            "key": "canonical",
            "message": "guide",
            "expectedTurnId": "turn-1",
            "clientRequestId": "request-1",
            "clientMessageId": "client-1",
        },
        durable=True,
    )

    admitted = ports.admit_call.await_args.args[0]
    assert admitted.session_key == "canonical"
    assert admitted.message == "hello"
    assert admitted.intent == "continue"
    assert admitted.intent_was_provided is True
    assert not hasattr(admitted, "attributes")
    cancelled = ports.cancel_call.await_args.args[0]
    assert cancelled.session_key == "canonical"
    assert cancelled.task_id == "task-1"
    assert cancelled.task_scoped is True
    assert cancelled.source == "test"
    steered = ports.steer_call.await_args.args[0]
    assert steered.session_key == "canonical"
    assert steered.message == "guide"
    assert steered.mode == "durable"
    assert steered.expected_turn_id == "turn-1"
    assert steered.client_request_id == "request-1"
    assert steered.client_message_id == "client-1"
    assert not hasattr(steered, "attributes")


async def test_adapter_preserves_task_scoped_abort_fence() -> None:
    adapter, ports = _adapter()

    result = await adapter.cancel(
        {"key": "canonical", "taskId": None, "scope": "task"},
        surface="webchat",
    )

    assert result == {
        "aborted": False,
        "key": "canonical",
        "reason": "task_id_required",
        "sessionKey": "canonical",
    }
    ports.cancel_call.assert_not_awaited()


@pytest.mark.parametrize(
    ("extra", "fingerprint", "explicit"),
    [
        ({}, "sha256:9b2d43affbf49a367028df2e1414f84c0e099ac98c3d54a8a80157fd7771af25", False),
        (
            {"intent": None},
            "sha256:9b2d43affbf49a367028df2e1414f84c0e099ac98c3d54a8a80157fd7771af25",
            False,
        ),
        (
            {"attachments": []},
            "sha256:9b2d43affbf49a367028df2e1414f84c0e099ac98c3d54a8a80157fd7771af25",
            False,
        ),
        (
            {"display_text": "legacy ignored"},
            "sha256:9b2d43affbf49a367028df2e1414f84c0e099ac98c3d54a8a80157fd7771af25",
            False,
        ),
        (
            {"intent": "continue"},
            "sha256:d4088f440e76746a4a0b2c09df4c558a2732ca814deaa6c418b1f53bd8a073c1",
            True,
        ),
        (
            {"intent": "new_chat", "collaborationMode": "plan"},
            "sha256:eb63daf4db4255848502bd5cd4e83ac018b1eedac0e305bcd061f4622d45adf0",
            True,
        ),
    ],
)
async def test_webchat_decoding_preserves_fingerprint_presence_semantics(
    extra, fingerprint, explicit
):
    adapter, ports = _adapter()
    await adapter.admit(
        {"sessionKey": "agent:main:webchat:one", "message": "hello", **extra},
        surface="webchat",
    )
    command = ports.admit_call.await_args.args[0]
    assert command.request_fingerprint == fingerprint
    assert command.intent_was_provided is explicit
    assert command.source_scope == "web:webchat:operator"
    assert command.source.caller_kind == "web"
    assert command.source.channel_kind == "webchat"
    assert command.source.channel_id == "webchat:agent:main:webchat:one"
    assert command.source.sender_id == "operator"
    assert not hasattr(command, "attributes")


async def test_webchat_decoder_does_not_infer_new_chat_from_workspace() -> None:
    adapter, ports = _adapter()
    await adapter.admit(
        {
            "sessionKey": "agent:main:webchat:one",
            "message": "hello",
            "workspaceId": "workspace-one",
        },
        surface="webchat",
    )
    command = ports.admit_call.await_args.args[0]
    assert command.intent == "continue"
    assert command.intent_was_provided is False
    assert command.workspace_id == "workspace-one"


async def test_admission_domain_error_retains_acceptance_and_retry_classification() -> None:
    adapter, ports = _adapter()
    ports.admit_call.side_effect = AdmissionError(
        "QUEUE_FULL_DIRTY",
        "durable input requires recovery",
        details={"orphan_message_id": "orphan-one", "fallback_safe": False},
        retryable=False,
        accepted=False,
    )
    with pytest.raises(RpcHandlerError) as caught:
        await adapter.admit({"key": "one", "message": "hello"}, surface="session")
    assert caught.value.code == "QUEUE_FULL_DIRTY"
    assert caught.value.details == {"orphan_message_id": "orphan-one", "fallback_safe": False}
    assert caught.value.retryable is False
    assert caught.value.accepted is False
    ports.admit_call.side_effect = AdmissionUnavailableError("durable storage is unavailable")
    with pytest.raises(RpcUnavailableError, match="durable storage is unavailable"):
        await adapter.admit({"key": "one", "message": "hello"}, surface="session")
