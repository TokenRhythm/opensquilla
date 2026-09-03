"""Gateway Adapter for the durable PendingInputQueue application Module."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from opensquilla.application.pending_input_queue import (
    PendingInputQueue,
    PendingInputQueuePort,
    PendingInputRequest,
)
from opensquilla.gateway.rpc import RpcContext

type PendingInputExecutor = Callable[
    [dict[str, Any] | None, RpcContext], Awaitable[dict[str, Any]]
]
type SessionKeyReader = Callable[[dict[str, Any] | None], str]


@dataclass(frozen=True, slots=True)
class GatewayPendingInputQueueCallbacks:
    require_key: SessionKeyReader
    enqueue: PendingInputExecutor
    list: PendingInputExecutor
    update: PendingInputExecutor
    reorder: PendingInputExecutor
    cancel: PendingInputExecutor
    dispatch: PendingInputExecutor
    steer: PendingInputExecutor


class GatewayPendingInputQueueRuntime(PendingInputQueuePort):
    """Request-scoped Port that terminates ``RpcContext``."""

    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewayPendingInputQueueCallbacks,
    ) -> None:
        self._context = context
        self._callbacks = callbacks

    @staticmethod
    def _params(request: PendingInputRequest) -> dict[str, Any]:
        params = dict(request.attributes)
        params["key"] = request.session_key
        if request.pending_input_id is not None:
            params["pendingInputId"] = request.pending_input_id
        if request.expected_revision is not None:
            params["expectedRevision"] = request.expected_revision
        return params

    async def enqueue(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self._callbacks.enqueue(self._params(request), self._context)

    async def list(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self._callbacks.list(self._params(request), self._context)

    async def update(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self._callbacks.update(self._params(request), self._context)

    async def reorder(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self._callbacks.reorder(self._params(request), self._context)

    async def cancel(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self._callbacks.cancel(self._params(request), self._context)

    async def dispatch(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self._callbacks.dispatch(self._params(request), self._context)

    async def steer(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self._callbacks.steer(self._params(request), self._context)


class GatewayPendingInputQueueAdapter:
    """Project wire aliases to the seven explicit queue use cases."""

    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewayPendingInputQueueCallbacks,
    ) -> None:
        self._callbacks = callbacks
        self._application = PendingInputQueue(
            GatewayPendingInputQueueRuntime(context, callbacks)
        )

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
        return PendingInputRequest(
            session_key=self._callbacks.require_key(params),
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
    "GatewayPendingInputQueueCallbacks",
    "GatewayPendingInputQueueRuntime",
]
