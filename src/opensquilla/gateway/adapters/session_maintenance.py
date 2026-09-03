"""Gateway Adapter for the transport-neutral SessionMaintenance Module."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from opensquilla.application.session_maintenance import (
    CompactSession,
    SessionMaintenance,
    SessionMaintenanceRuntimePort,
)
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError

type MaintenanceExecutor = Callable[
    [dict[str, Any] | None, RpcContext], Awaitable[dict[str, Any]]
]
type SessionKeyReader = Callable[[dict[str, Any] | None], str]


@dataclass(frozen=True, slots=True)
class GatewaySessionMaintenanceCallbacks:
    require_key: SessionKeyReader
    execute_compact: MaintenanceExecutor


class GatewaySessionMaintenanceRuntime(SessionMaintenanceRuntimePort):
    """Request-scoped runtime Port; the complete RpcContext terminates here."""

    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewaySessionMaintenanceCallbacks,
    ) -> None:
        self._context = context
        self._callbacks = callbacks

    async def compact(self, command: CompactSession) -> Mapping[str, Any]:
        params: dict[str, Any] = {
            "key": command.session_key,
            "wait": command.wait,
        }
        if command.context_window_tokens is not None:
            params["contextWindowTokens"] = command.context_window_tokens
        if command.instructions is not None:
            params["instructions"] = command.instructions
        return await self._callbacks.execute_compact(params, self._context)


class GatewaySessionMaintenanceAdapter:
    """Translate v4 request fields and project application results."""

    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewaySessionMaintenanceCallbacks,
    ) -> None:
        self._callbacks = callbacks
        self._application = SessionMaintenance(
            GatewaySessionMaintenanceRuntime(context, callbacks)
        )

    async def compact(self, params: dict[str, Any] | None) -> dict[str, Any]:
        key = self._callbacks.require_key(params)
        raw = params or {}
        instructions = raw.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise RpcHandlerError(
                code="INVALID_PARAMS",
                message="instructions must be a string when provided.",
                details={"field": "instructions"},
            )
        context_window_tokens = raw.get("contextWindowTokens")
        if context_window_tokens is not None:
            try:
                context_window_tokens = int(context_window_tokens)
            except (TypeError, ValueError) as exc:
                raise RpcHandlerError(
                    code="INVALID_PARAMS",
                    message="contextWindowTokens must be a positive integer.",
                    details={"field": "contextWindowTokens"},
                ) from exc
        try:
            result = await self._application.compact(
                CompactSession(
                    session_key=key,
                    wait=bool(raw.get("wait", True)),
                    context_window_tokens=context_window_tokens,
                    instructions=instructions,
                )
            )
        except ValueError as exc:
            raise RpcHandlerError(
                code="INVALID_PARAMS",
                message=str(exc),
                details={"field": "contextWindowTokens"},
            ) from exc
        return dict(result)


__all__ = [
    "GatewaySessionMaintenanceAdapter",
    "GatewaySessionMaintenanceCallbacks",
    "GatewaySessionMaintenanceRuntime",
]
