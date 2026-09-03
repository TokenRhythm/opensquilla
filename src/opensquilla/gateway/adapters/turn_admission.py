"""Gateway Adapter for the transport-neutral TurnAdmission Module."""

from __future__ import annotations

from typing import Any, cast

from opensquilla.application.turn_admission import (
    AdmitTurn,
    CancelTurn,
    InitialCollaborationMode,
    InitialRoutingMode,
    PendingInputGuard,
    SteerTurn,
    TurnAdmission,
)
from opensquilla.session.keys import build_webchat_key, canonicalize_session_key

_WEBCHAT_SESSION_KEY = build_webchat_key()


def webchat_session_key(value: object = None) -> str:
    """Map retained WebChat defaults onto the canonical session identity."""
    raw = str(value or "").strip()
    if not raw or raw in {"default", "webchat:default", "unknown"}:
        return _WEBCHAT_SESSION_KEY
    if raw.startswith("sess-"):
        return f"agent:main:webchat:{raw[len('sess-') :]}"
    return canonicalize_session_key(raw)


class GatewayTurnAdmissionAdapter:
    """Translate v4 request fields into semantic turn commands."""

    def __init__(
        self,
        application: TurnAdmission,
    ) -> None:
        self._application = application

    @staticmethod
    def _key(params: dict[str, Any] | None, *, surface: str) -> str:
        raw = params if isinstance(params, dict) else {}
        if surface == "webchat":
            return webchat_session_key(
                raw.get("sessionKey", raw.get("session_key", raw.get("key")))
            )
        if "key" not in raw:
            raise ValueError("params.key is required")
        key = raw["key"]
        if not isinstance(key, str):
            raise ValueError("params.key must be a string")
        return canonicalize_session_key(key)

    @staticmethod
    def _initial_collaboration_mode(
        params: dict[str, Any],
    ) -> InitialCollaborationMode | None:
        mode = params.get("collaborationMode")
        snake_mode = params.get("collaboration_mode")
        if mode is not None and snake_mode is not None and mode != snake_mode:
            raise ValueError("collaborationMode and collaboration_mode must match")
        if mode is None:
            mode = snake_mode
        if mode is None:
            return None
        if not isinstance(mode, str) or mode not in {"default", "plan"}:
            raise ValueError("collaborationMode must be default or plan")
        if params.get("intent") != "new_chat":
            raise ValueError("collaborationMode requires explicit new_chat intent")
        return cast(InitialCollaborationMode, mode)

    @staticmethod
    def _initial_routing_mode(
        params: dict[str, Any],
    ) -> InitialRoutingMode | None:
        mode = params.get("initialRoutingMode")
        snake_mode = params.get("initial_routing_mode")
        if mode is not None and snake_mode is not None and mode != snake_mode:
            raise ValueError("initialRoutingMode and initial_routing_mode must match")
        if mode is None:
            mode = snake_mode
        if mode is None:
            return None
        if not isinstance(mode, str) or mode not in {"direct", "router", "ensemble"}:
            raise ValueError("initialRoutingMode must be direct, router, or ensemble")
        if params.get("intent") != "new_chat":
            raise ValueError("initialRoutingMode requires explicit new_chat intent")
        return cast(InitialRoutingMode, mode)

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
        key = self._key(params, surface=surface)
        result = await self._application.admit(
            AdmitTurn(
                session_key=key,
                message=params["message"],
                surface="webchat" if surface == "webchat" else "session",
                attributes=self._attributes(params),
                initial_collaboration_mode=(
                    self._initial_collaboration_mode(params)
                    if surface == "webchat"
                    else None
                ),
                initial_routing_mode=(
                    self._initial_routing_mode(params)
                    if surface == "webchat"
                    else None
                ),
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
        key = self._key(params, surface=surface)
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
        key = self._key(params, surface="session")
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
        key = self._key(params, surface="session")
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
        key = self._key(params, surface="session")
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
    "webchat_session_key",
]
