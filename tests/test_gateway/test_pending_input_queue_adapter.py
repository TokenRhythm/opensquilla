from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from opensquilla.gateway.adapters.pending_input_queue import (
    GatewayPendingInputQueueAdapter,
    GatewayPendingInputQueueCallbacks,
)
from opensquilla.gateway.rpc import RpcContext


def _adapter() -> tuple[GatewayPendingInputQueueAdapter, dict[str, AsyncMock], RpcContext]:
    callbacks = {
        name: AsyncMock(return_value={"status": name})
        for name in ("enqueue", "list", "update", "reorder", "cancel", "dispatch", "steer")
    }
    context = cast(RpcContext, SimpleNamespace())
    adapter = GatewayPendingInputQueueAdapter(
        context,
        GatewayPendingInputQueueCallbacks(
            require_key=lambda params: str((params or {})["key"]),
            enqueue=callbacks["enqueue"],
            list=callbacks["list"],
            update=callbacks["update"],
            reorder=callbacks["reorder"],
            cancel=callbacks["cancel"],
            dispatch=callbacks["dispatch"],
            steer=callbacks["steer"],
        ),
    )
    return adapter, callbacks, context


async def test_adapter_preserves_queue_identity_and_revision_aliases() -> None:
    adapter, callbacks, context = _adapter()

    result = await adapter.update(
        {
            "key": "agent:main:webchat:one",
            "pending_input_id": "pending-1",
            "expected_revision": 2,
            "position": 1,
        }
    )

    assert result == {"status": "update"}
    callbacks["update"].assert_awaited_once_with(
        {
            "key": "agent:main:webchat:one",
            "pending_input_id": "pending-1",
            "pendingInputId": "pending-1",
            "expected_revision": 2,
            "expectedRevision": 2,
            "position": 1,
        },
        context,
    )


async def test_adapter_exposes_all_seven_queue_use_cases() -> None:
    adapter, callbacks, _context = _adapter()
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

    assert all(callback.await_count == 1 for callback in callbacks.values())
