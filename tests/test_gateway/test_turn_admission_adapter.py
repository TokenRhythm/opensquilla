from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from opensquilla.application.turn_admission import (
    AdmitTurn,
    CancelTurn,
    SteerTurn,
    TurnAdmission,
)
from opensquilla.gateway.adapters.turn_admission import (
    GatewayTurnAdmissionAdapter,
)


class _Ports:
    def __init__(self) -> None:
        self.admit_call = AsyncMock(
            return_value={"status": "accepted", "key": "canonical"}
        )
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
        },
        durable=True,
    )

    admitted = ports.admit_call.await_args.args[0]
    assert admitted.session_key == "canonical"
    assert admitted.message == "hello"
    assert admitted.attributes == {"intent": "continue"}
    cancelled = ports.cancel_call.await_args.args[0]
    assert cancelled.session_key == "canonical"
    assert cancelled.task_id == "task-1"
    assert cancelled.task_scoped is True
    assert cancelled.source == "test"
    steered = ports.steer_call.await_args.args[0]
    assert steered.session_key == "canonical"
    assert steered.message == "guide"
    assert steered.mode == "durable"
    assert steered.attributes == {"expectedTurnId": "turn-1"}


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
