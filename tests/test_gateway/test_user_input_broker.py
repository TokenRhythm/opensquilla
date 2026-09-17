from __future__ import annotations

import asyncio

import pytest

from opensquilla.gateway.user_input_broker import (
    StructuredUserInputBroker,
    UserInputRequestConflictError,
    UserInputValidationError,
)

_SESSION_KEY = "agent:main:webchat:user-input"


def _payload() -> dict:
    return {
        "status": "input_required",
        "kind": "user_input",
        "paused": True,
        "clarify_schema": {
            "mode": "form",
            "fields": [
                {
                    "name": "scope",
                    "type": "enum",
                    "required": True,
                    "choices": ["Core", "Full"],
                    "allow_other": True,
                },
                {
                    "name": "verify",
                    "type": "bool",
                    "required": True,
                },
            ],
        },
    }


@pytest.mark.asyncio
async def test_broker_hydrates_pending_then_resolves_exact_waiter() -> None:
    broker = StructuredUserInputBroker()
    public = broker.open_request(
        session_key=_SESSION_KEY,
        task_id="task-1",
        tool_use_id="tool-1",
        payload=_payload(),
    )

    assert public["request_id"]
    assert public["run_id"] == "task-1"
    assert broker.pending_for_session(_SESSION_KEY) == [public]

    waiter = asyncio.create_task(broker.wait_for_response(public["request_id"]))
    result = broker.resolve(
        session_key=_SESSION_KEY,
        request_id=public["request_id"],
        fields={"scope": "A narrower custom scope", "verify": "true"},
    )

    assert result == {
        "resolved": True,
        "replayed": False,
        "request_id": public["request_id"],
    }
    assert await waiter == {
        "scope": "A narrower custom scope",
        "verify": True,
    }
    assert broker.pending_for_session(_SESSION_KEY) == []


@pytest.mark.asyncio
async def test_broker_rejects_invalid_cross_session_and_conflicting_replay() -> None:
    broker = StructuredUserInputBroker()
    public = broker.open_request(
        session_key=_SESSION_KEY,
        task_id="task-1",
        tool_use_id="tool-1",
        payload=_payload(),
    )
    request_id = public["request_id"]

    with pytest.raises(UserInputRequestConflictError):
        broker.resolve(
            session_key="agent:main:webchat:other",
            request_id=request_id,
            fields={"scope": "Core", "verify": True},
        )
    with pytest.raises(UserInputValidationError):
        broker.resolve(
            session_key=_SESSION_KEY,
            request_id=request_id,
            fields={"scope": "Core"},
        )

    waiter = asyncio.create_task(broker.wait_for_response(request_id))
    broker.resolve(
        session_key=_SESSION_KEY,
        request_id=request_id,
        fields={"scope": "Core", "verify": True},
    )
    assert await waiter == {"scope": "Core", "verify": True}
    assert broker.resolve(
        session_key=_SESSION_KEY,
        request_id=request_id,
        fields={"scope": "Core", "verify": "true"},
    )["replayed"] is True
    with pytest.raises(UserInputRequestConflictError):
        broker.resolve(
            session_key=_SESSION_KEY,
            request_id=request_id,
            fields={"scope": "Full", "verify": True},
        )


@pytest.mark.asyncio
async def test_broker_cancels_waiter_with_owning_task() -> None:
    broker = StructuredUserInputBroker()
    public = broker.open_request(
        session_key=_SESSION_KEY,
        task_id="task-1",
        tool_use_id="tool-1",
        payload=_payload(),
    )
    waiter = asyncio.create_task(broker.wait_for_response(public["request_id"]))
    await asyncio.sleep(0)

    broker.cancel_task("task-1")

    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert broker.pending_for_session(_SESSION_KEY) == []
