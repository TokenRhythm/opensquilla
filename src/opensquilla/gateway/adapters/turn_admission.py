"""Gateway Adapter for the transport-neutral TurnAdmission Module."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from opensquilla.application.turn_admission import (
    AdmitTurn,
    CancelTurn,
    PendingInputGuard,
    SteerTurn,
    TurnAdmission,
    TurnAdmissionRuntimePort,
)
from opensquilla.gateway.rpc import RpcContext

type TurnExecutor = Callable[
    [dict[str, Any] | None, RpcContext], Awaitable[dict[str, Any]]
]
type SessionKeyReader = Callable[[dict[str, Any] | None], str]
type PendingTurnExecutor = Callable[
    [dict[str, Any], RpcContext, PendingInputGuard], Awaitable[dict[str, Any]]
]


@dataclass(frozen=True, slots=True)
class GatewayTurnAdmissionCallbacks:
    require_key: SessionKeyReader
    execute_chat_send: TurnExecutor | None = None
    execute_session_send: TurnExecutor | None = None
    execute_chat_abort: TurnExecutor | None = None
    execute_session_abort: TurnExecutor | None = None
    execute_durable_steer: TurnExecutor | None = None
    execute_legacy_steer: TurnExecutor | None = None
    execute_pending_send: PendingTurnExecutor | None = None
    execute_pending_steer: PendingTurnExecutor | None = None


def _executor(value: TurnExecutor | None, operation: str) -> TurnExecutor:
    if value is None:
        raise RuntimeError(f"TurnAdmission runtime does not provide {operation}")
    return value


class GatewayTurnAdmissionRuntime(TurnAdmissionRuntimePort):
    """Request-scoped Port; the complete ``RpcContext`` terminates here."""

    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewayTurnAdmissionCallbacks,
    ) -> None:
        self._context = context
        self._callbacks = callbacks

    @staticmethod
    def _params(
        session_key: str,
        message: str | None,
        attributes: Mapping[str, Any],
    ) -> dict[str, Any]:
        params = dict(attributes)
        params["key"] = session_key
        if message is not None:
            params["message"] = message
        return params

    async def admit(self, command: AdmitTurn) -> Mapping[str, Any]:
        if command.pending_input is not None:
            pending_callback = self._callbacks.execute_pending_send
            if pending_callback is None:
                raise RuntimeError("TurnAdmission runtime does not provide pending send")
            return await pending_callback(
                self._params(command.session_key, command.message, command.attributes),
                self._context,
                command.pending_input,
            )
        callback = _executor(
            self._callbacks.execute_chat_send
            if command.surface == "webchat"
            else self._callbacks.execute_session_send,
            f"{command.surface} send",
        )
        params = self._params(command.session_key, command.message, command.attributes)
        if command.surface == "webchat":
            params["sessionKey"] = command.session_key
        return await callback(params, self._context)

    async def cancel(self, command: CancelTurn) -> Mapping[str, Any]:
        callback = _executor(
            self._callbacks.execute_chat_abort
            if command.surface == "webchat"
            else self._callbacks.execute_session_abort,
            f"{command.surface} abort",
        )
        params = self._params(command.session_key, None, command.attributes)
        if command.surface == "webchat":
            params["sessionKey"] = command.session_key
        if command.task_id is not None:
            params["task_id"] = command.task_id
        if command.task_scoped:
            params["scope"] = "task"
        if command.source is not None:
            params["source"] = command.source
        return await callback(params, self._context)

    async def steer(self, command: SteerTurn) -> Mapping[str, Any]:
        if command.pending_input is not None:
            pending_callback = self._callbacks.execute_pending_steer
            if pending_callback is None:
                raise RuntimeError("TurnAdmission runtime does not provide pending steer")
            return await pending_callback(
                self._params(command.session_key, command.message, command.attributes),
                self._context,
                command.pending_input,
            )
        callback = _executor(
            self._callbacks.execute_durable_steer
            if command.mode == "durable"
            else self._callbacks.execute_legacy_steer,
            f"{command.mode} steer",
        )
        return await callback(
            self._params(command.session_key, command.message, command.attributes),
            self._context,
        )


class GatewayTurnAdmissionAdapter:
    """Translate v4 request fields into semantic turn commands."""

    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewayTurnAdmissionCallbacks,
    ) -> None:
        self._callbacks = callbacks
        self._application = TurnAdmission(GatewayTurnAdmissionRuntime(context, callbacks))

    @staticmethod
    def _attributes(params: dict[str, Any]) -> dict[str, Any]:
        attributes = dict(params)
        for key in ("key", "sessionKey", "session_key", "message"):
            attributes.pop(key, None)
        return attributes

    async def admit(
        self,
        params: dict[str, Any] | None,
        *,
        surface: str,
    ) -> dict[str, Any]:
        if not isinstance(params, dict) or "message" not in params:
            raise ValueError("params.message is required")
        key = self._callbacks.require_key(params)
        result = await self._application.admit(
            AdmitTurn(
                session_key=key,
                message=params["message"],
                surface="webchat" if surface == "webchat" else "session",
                attributes=self._attributes(params),
            )
        )
        return dict(result)

    async def cancel(
        self,
        params: dict[str, Any] | None,
        *,
        surface: str,
    ) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        key = self._callbacks.require_key(params)
        raw_task_id = raw.get("task_id", raw.get("taskId"))
        task_id = raw_task_id if isinstance(raw_task_id, str) else None
        task_scoped = "task_id" in raw or "taskId" in raw or (
            isinstance(raw.get("scope"), str) and raw["scope"].strip().lower() == "task"
        )
        source = raw.get("source") if isinstance(raw.get("source"), str) else None
        result = await self._application.cancel(
            CancelTurn(
                session_key=key,
                surface="webchat" if surface == "webchat" else "session",
                task_id=task_id,
                task_scoped=task_scoped,
                source=source,
                attributes=self._attributes(raw),
            )
        )
        projected = dict(result)
        if surface == "webchat":
            projected.setdefault("sessionKey", key)
        return projected

    async def steer(
        self,
        params: dict[str, Any] | None,
        *,
        durable: bool,
    ) -> dict[str, Any]:
        if not isinstance(params, dict) or "message" not in params:
            raise ValueError("params.message is required")
        key = self._callbacks.require_key(params)
        result = await self._application.steer(
            SteerTurn(
                session_key=key,
                message=params["message"],
                mode="durable" if durable else "legacy",
                attributes=self._attributes(params),
            )
        )
        return dict(result)

    async def admit_pending(
        self,
        params: dict[str, Any],
        guard: PendingInputGuard,
    ) -> dict[str, Any]:
        if "message" not in params:
            raise ValueError("params.message is required")
        key = self._callbacks.require_key(params)
        result = await self._application.admit(
            AdmitTurn(
                session_key=key,
                message=params["message"],
                surface="session",
                attributes=self._attributes(params),
                pending_input=guard,
            )
        )
        return dict(result)

    async def steer_pending(
        self,
        params: dict[str, Any],
        guard: PendingInputGuard,
    ) -> dict[str, Any]:
        if "message" not in params:
            raise ValueError("params.message is required")
        key = self._callbacks.require_key(params)
        result = await self._application.steer(
            SteerTurn(
                session_key=key,
                message=params["message"],
                mode="durable",
                attributes=self._attributes(params),
                pending_input=guard,
            )
        )
        return dict(result)


__all__ = [
    "GatewayTurnAdmissionAdapter",
    "GatewayTurnAdmissionCallbacks",
    "GatewayTurnAdmissionRuntime",
]
