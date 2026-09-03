"""Gateway Adapter for the durable PendingInputQueue application Module."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opensquilla.application.pending_input_queue import (
    PendingInputQueue,
    PendingInputQueuePort,
    PendingInputRequest,
)


class GatewayPendingInputQueueAdapter:
    """Project wire aliases to the seven explicit queue use cases."""

    def __init__(
        self,
        port: PendingInputQueuePort,
    ) -> None:
        self._application = PendingInputQueue(port)

    @staticmethod
    def _optional_string(raw: Mapping[str, Any], *names: str) -> str | None:
        for name in names:
            value = raw.get(name)
            if isinstance(value, str):
                return value
        return None

    @staticmethod
    def _optional_revision(raw: Mapping[str, Any]) -> int | None:
        value = raw.get("expectedRevision", raw.get("expected_revision"))
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _request(self, params: dict[str, Any] | None) -> PendingInputRequest:
        raw = params if isinstance(params, dict) else {}
        key = raw.get("key", raw.get("sessionKey"))
        if not isinstance(key, str) or not key.strip():
            raise ValueError("params.key is required")
        return PendingInputRequest(
            session_key=key,
            pending_input_id=self._optional_string(
                raw, "pendingInputId", "pending_input_id"
            ),
            expected_revision=self._optional_revision(raw),
            attributes=raw,
        )

    async def enqueue(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._application.enqueue(self._request(params)))

    async def list(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._application.list(self._request(params)))

    async def update(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._application.update(self._request(params)))

    async def reorder(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._application.reorder(self._request(params)))

    async def cancel(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._application.cancel(self._request(params)))

    async def dispatch(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._application.dispatch(self._request(params)))

    async def steer(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._application.steer(self._request(params)))


__all__ = [
    "GatewayPendingInputQueueAdapter",
]
