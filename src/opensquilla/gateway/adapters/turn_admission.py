"""Gateway Adapter for the transport-neutral TurnAdmission Module."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

from opensquilla.application.admission_errors import AdmissionError, AdmissionUnavailableError
from opensquilla.application.admission_failures import AdmissionStorageBusyError
from opensquilla.application.turn_admission import (
    AdmitTurn,
    CancelTurn,
    InitialCollaborationMode,
    InitialRoutingMode,
    TurnAdmission,
)
from opensquilla.chat.conversation import ChatSendRequest, sessions_send_params
from opensquilla.chat.source import chat_source_metadata
from opensquilla.gateway.admission_input import decode_admit_turn
from opensquilla.gateway.rpc import RpcHandlerError, RpcUnavailableError
from opensquilla.gateway.turn_steering import decode_steering_command, map_steering_error
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


def map_admission_error(error: Exception) -> Exception:
    """Project admission failures at the Gateway, including pending-queue dispatch."""
    if isinstance(error, AdmissionUnavailableError):
        return RpcUnavailableError(str(error))
    if isinstance(error, AdmissionStorageBusyError):
        # Read/replay and conflict rereads can fail outside a commit phase.
        # Preserve the registry's existing retry response, without claiming
        # whether a prior durable write was accepted.
        details: dict[str, Any] = {
            "operation": error.operation,
            "waited_ms": error.waited_ms,
        }
        if error.stage is not None:
            details["stage"] = error.stage
        if error.resource is not None:
            details["resource"] = error.resource
        return RpcHandlerError(
            "STORAGE_BUSY",
            "Session storage is temporarily busy. Retry this operation.",
            retryable=True,
            retry_after_ms=error.retry_after_ms,
            details=details,
        )
    if isinstance(error, AdmissionError):
        return RpcHandlerError(
            error.kind,
            error.message,
            details=error.details,
            retryable=error.retryable if error.retryable is not None else False,
            retry_after_ms=error.retry_after_ms,
            accepted=error.accepted,
        )
    return error


class GatewayTurnAdmissionAdapter:
    """Translate v4 request fields into semantic turn commands."""

    def __init__(
        self,
        application: TurnAdmission,
        *,
        principal_role: str = "operator",
        connection_id: str = "",
        is_owner: bool = False,
    ) -> None:
        self._application = application
        self._principal_role = principal_role
        self._connection_id = connection_id
        self._is_owner = is_owner

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

    def _webchat_command(self, params: dict[str, Any], key: str) -> AdmitTurn:
        collaboration = self._initial_collaboration_mode(params)
        routing = self._initial_routing_mode(params)
        raw_ids = params.get("promptAnnotationIds", params.get("prompt_annotation_ids"))
        ids: list[str] | None = None
        if raw_ids is not None:
            if not isinstance(raw_ids, list):
                raise ValueError("params.promptAnnotationIds must be an array")
            if any(not isinstance(item, str) or not item.strip() for item in raw_ids):
                raise ValueError("params.promptAnnotationIds must contain non-empty strings")
            ids = [item.strip() for item in raw_ids]
        incoming_source = params.get("_source")
        incoming_source = incoming_source if isinstance(incoming_source, dict) else {}
        elevated = incoming_source.get("elevated")
        run_mode = incoming_source.get("runMode") or incoming_source.get("run_mode")
        extra: dict[str, Any] = {}
        for name in (
            "noMemoryCapture",
            "no_memory_capture",
            "inputProvenance",
            "input_provenance",
            "inputProvenanceKind",
            "input_provenance_kind",
            "provenance_kind",
            "runKind",
            "run_kind",
            "queueMode",
            "queue_mode",
            "forkBeforeMessageId",
            "fork_before_message_id",
            "clientRequestId",
            "client_request_id",
            "clientMessageId",
            "client_message_id",
            "surfaceId",
            "surface_id",
            "workspaceId",
            "workspace_id",
            "promptAnnotationIds",
            "prompt_annotation_ids",
            "documentContext",
            "document_context",
            "initialRoutingMode",
            "initial_routing_mode",
        ):
            if name in params:
                target = {
                    "prompt_annotation_ids": "promptAnnotationIds",
                    "document_context": "documentContext",
                }.get(name, name)
                extra[target] = params[name]
        attachments = params.get("attachments")
        projected = sessions_send_params(
            ChatSendRequest(
                session_key=key,
                message=params["message"],
                attachments=attachments if isinstance(attachments, list) else [],
                display_text=params.get("displayText"),
                intent=params.get("intent"),
                extra=extra,
            ),
            chat_source_metadata(
                caller_kind="web",
                channel_kind="webchat",
                channel_id=f"webchat:{key}",
                sender_id=self._principal_role,
                source_kind="webui",
                source_name="WebChat",
                elevated=elevated if isinstance(elevated, str) else None,
                run_mode=run_mode if isinstance(run_mode, str) else None,
            ),
        )
        fingerprint = dict(projected)
        if params.get("intent") is None:
            fingerprint.pop("intent", None)
        if collaboration is not None:
            fingerprint["initialCollaborationMode"] = collaboration
        if routing is not None:
            fingerprint["initialRoutingMode"] = routing
        if ids is not None:
            projected["promptAnnotationIds"] = ids
            fingerprint["promptAnnotationIds"] = ids
        return replace(
            decode_admit_turn(
                projected,
                surface="webchat",
                principal_role=self._principal_role,
                connection_id=self._connection_id,
                fingerprint_params=fingerprint,
            ),
            initial_collaboration_mode=collaboration,
            initial_routing_mode=routing,
            intent_was_provided=params.get("intent") is not None,
        )

    async def admit(
        self,
        params: dict[str, Any] | None,
        *,
        surface: str,
    ) -> dict[str, Any]:
        if not isinstance(params, dict) or "message" not in params:
            raise ValueError("params.message is required")
        key = self._key(params, surface=surface)
        command = (
            self._webchat_command(params, key)
            if surface == "webchat"
            else decode_admit_turn(
                {**params, "key": key},
                surface="session",
                principal_role=self._principal_role,
                connection_id=self._connection_id,
            )
        )
        try:
            result = await self._application.admit(command)
        except Exception as exc:
            mapped = map_admission_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc
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
        task_scoped = (
            "task_id" in raw
            or "taskId" in raw
            or (isinstance(raw.get("scope"), str) and raw["scope"].strip().lower() == "task")
        )
        source = raw.get("source") if isinstance(raw.get("source"), str) else None
        result = await self._application.cancel(
            CancelTurn(
                session_key=key,
                surface="webchat" if surface == "webchat" else "session",
                task_id=task_id,
                task_scoped=task_scoped,
                source=source,
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
        command = decode_steering_command(
            params,
            key=key,
            durable=durable,
            principal_role=self._principal_role,
            connection_id=self._connection_id,
        )
        try:
            result = await self._application.steer(command)
        except Exception as exc:
            mapped = map_steering_error(exc, is_owner=self._is_owner)
            if mapped is exc:
                raise
            raise mapped from exc
        return dict(result)

__all__ = [
    "GatewayTurnAdmissionAdapter",
    "webchat_session_key",
]
