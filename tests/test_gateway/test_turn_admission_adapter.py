from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from opensquilla.gateway.adapters.turn_admission import (
    GatewayTurnAdmissionAdapter,
    GatewayTurnAdmissionCallbacks,
)
from opensquilla.gateway.rpc import RpcContext


def _adapter() -> tuple[
    GatewayTurnAdmissionAdapter,
    RpcContext,
    AsyncMock,
    AsyncMock,
    AsyncMock,
]:
    session_send = AsyncMock(return_value={"status": "accepted", "key": "canonical"})
    session_abort = AsyncMock(return_value={"aborted": True, "key": "canonical"})
    durable_steer = AsyncMock(return_value={"accepted": True, "key": "canonical"})
    context = cast(RpcContext, SimpleNamespace())
    adapter = GatewayTurnAdmissionAdapter(
        context,
        GatewayTurnAdmissionCallbacks(
            require_key=lambda params: str((params or {})["key"]),
            execute_session_send=session_send,
            execute_session_abort=session_abort,
            execute_durable_steer=durable_steer,
        ),
    )
    return adapter, context, session_send, session_abort, durable_steer


async def test_adapter_projects_semantic_commands_to_existing_runtime() -> None:
    adapter, context, session_send, session_abort, durable_steer = _adapter()

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

    session_send.assert_awaited_once_with(
        {"key": "canonical", "message": "hello", "intent": "continue"},
        context,
    )
    session_abort.assert_awaited_once_with(
        {
            "key": "canonical",
            "taskId": "task-1",
            "scope": "task",
            "source": "test",
            "task_id": "task-1",
        },
        context,
    )
    durable_steer.assert_awaited_once_with(
        {"key": "canonical", "message": "guide", "expectedTurnId": "turn-1"},
        context,
    )


async def test_adapter_preserves_task_scoped_abort_fence() -> None:
    adapter, _context, _session_send, session_abort, _durable_steer = _adapter()

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
    session_abort.assert_not_awaited()
