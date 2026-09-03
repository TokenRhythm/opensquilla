from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock

from opensquilla.application.pending_input_queue import PendingInputRequest
from opensquilla.gateway.adapters.pending_input_queue import GatewayPendingInputQueueAdapter


class _RecordingPort:
    def __init__(self) -> None:
        self.calls = {
            name: AsyncMock(return_value={"status": name})
            for name in ("enqueue", "list", "update", "reorder", "cancel", "dispatch", "steer")
        }

    async def enqueue(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self.calls["enqueue"](request)

    async def list(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self.calls["list"](request)

    async def update(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self.calls["update"](request)

    async def reorder(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self.calls["reorder"](request)

    async def cancel(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self.calls["cancel"](request)

    async def dispatch(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self.calls["dispatch"](request)

    async def steer(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self.calls["steer"](request)


def _adapter() -> tuple[GatewayPendingInputQueueAdapter, _RecordingPort]:
    port = _RecordingPort()
    return GatewayPendingInputQueueAdapter(port), port


async def test_adapter_preserves_queue_identity_and_revision_aliases() -> None:
    adapter, port = _adapter()

    result = await adapter.update(
        {
            "key": "agent:main:webchat:one",
            "pending_input_id": "pending-1",
            "expected_revision": 2,
            "position": 1,
        }
    )

    assert result == {"status": "update"}
    request = port.calls["update"].await_args.args[0]
    assert request == PendingInputRequest(
        session_key="agent:main:webchat:one",
        pending_input_id="pending-1",
        expected_revision=2,
        attributes={
            "key": "agent:main:webchat:one",
            "pending_input_id": "pending-1",
            "expected_revision": 2,
            "position": 1,
        },
    )


async def test_adapter_exposes_all_seven_queue_use_cases() -> None:
    adapter, port = _adapter()
    key = "agent:main:webchat:one"
    identified = {"key": key, "pendingInputId": "pending-1"}
    revisioned = {**identified, "expectedRevision": 1}

    await adapter.enqueue(identified)
    await adapter.list({"key": key})
    await adapter.update({**revisioned, "position": 0})
    await adapter.reorder({"key": key, "items": []})
    await adapter.cancel(identified)
    await adapter.dispatch(identified)
    await adapter.steer(revisioned)

    assert all(call.await_count == 1 for call in port.calls.values())
