from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from opensquilla.application.pending_input_queue import (
    PendingInputQueue,
    PendingInputRequest,
)


@dataclass
class _Port:
    calls: list[tuple[str, PendingInputRequest]] = field(default_factory=list)

    async def _call(self, name: str, request: PendingInputRequest) -> dict[str, Any]:
        self.calls.append((name, request))
        return {"operation": name, "sessionKey": request.session_key}

    async def enqueue(self, request: PendingInputRequest) -> dict[str, Any]:
        return await self._call("enqueue", request)

    async def list(self, request: PendingInputRequest) -> dict[str, Any]:
        return await self._call("list", request)

    async def update(self, request: PendingInputRequest) -> dict[str, Any]:
        return await self._call("update", request)

    async def reorder(self, request: PendingInputRequest) -> dict[str, Any]:
        return await self._call("reorder", request)

    async def cancel(self, request: PendingInputRequest) -> dict[str, Any]:
        return await self._call("cancel", request)

    async def dispatch(self, request: PendingInputRequest) -> dict[str, Any]:
        return await self._call("dispatch", request)

    async def steer(self, request: PendingInputRequest) -> dict[str, Any]:
        return await self._call("steer", request)


async def test_queue_canonicalizes_identity_for_all_explicit_use_cases() -> None:
    port = _Port()
    queue = PendingInputQueue(port)
    base = PendingInputRequest(
        " agent:main:webchat:one ",
        {},
        pending_input_id=" pending-1 ",
        expected_revision=2,
    )

    await queue.enqueue(base)
    await queue.list(PendingInputRequest(base.session_key, {}))
    await queue.update(base)
    await queue.reorder(PendingInputRequest(base.session_key, {"items": []}))
    await queue.cancel(base)
    await queue.dispatch(base)
    await queue.steer(base)

    assert [name for name, _request in port.calls] == [
        "enqueue", "list", "update", "reorder", "cancel", "dispatch", "steer"
    ]
    assert all(
        request.session_key == "agent:main:webchat:one"
        for _name, request in port.calls
    )
    assert port.calls[0][1].pending_input_id == "pending-1"


async def test_revision_guard_rejects_update_before_storage() -> None:
    port = _Port()
    queue = PendingInputQueue(port)

    with pytest.raises(ValueError, match="expected_revision"):
        await queue.update(
            PendingInputRequest(
                "agent:main:webchat:one",
                {},
                pending_input_id="pending-1",
                expected_revision=0,
            )
        )

    assert port.calls == []
