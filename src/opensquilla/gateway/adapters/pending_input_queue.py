"""Gateway Adapter for the durable PendingInputQueue application Module."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opensquilla.application.admission_failures import AdmissionPendingInputConflictError
from opensquilla.application.pending_input_queue import (
    CancelPendingInput,
    DispatchPendingInput,
    EnqueuePendingInput,
    MovePendingInput,
    PendingCancellationConflictError,
    PendingEnqueueRejectedError,
    PendingInputConflictError,
    PendingInputMissingError,
    PendingInputQueue,
    PendingInputQueuePort,
    PendingInputRevision,
    PendingMaterialRejectedError,
    PendingQueueRejectedError,
    ReorderPendingInputs,
    SteerPendingInput,
)
from opensquilla.application.turn_admission import TurnAdmission
from opensquilla.gateway.adapters.turn_admission import map_admission_error
from opensquilla.gateway.admission_input import (
    decode_admit_turn,
    decode_turn_source,
    source_scope_from_turn,
)
from opensquilla.gateway.rpc.registry import RpcHandlerError
from opensquilla.gateway.turn_steering import map_steering_error
from opensquilla.session.storage import PendingChatInputConflictError, PendingChatInputNotFoundError


class GatewayPendingInputQueueAdapter:
    """Project wire aliases to the seven explicit queue use cases."""

    def __init__(
        self,
        port: PendingInputQueuePort,
        *,
        turns: TurnAdmission | None = None,
        principal_role: str = "operator",
        is_owner: bool = False,
    ) -> None:
        self._application = PendingInputQueue(port, turns=turns)
        self._principal_role = principal_role
        self._is_owner = is_owner

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

    async def list(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._application.list(self._key(params)))

    async def update(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params or {}
        revision = self._optional_revision(raw)
        if revision is None:
            raise ValueError("expected_revision must be positive")
        position = raw.get("position")
        if isinstance(position, bool) or not isinstance(position, int):
            raise ValueError("params.position must be an integer")
        command = MovePendingInput(
            self._key(params),
            self._optional_string(raw, "pendingInputId", "pending_input_id") or "",
            revision,
            position,
        )
        try:
            return dict(await self._application.update(command))
        except PendingInputMissingError as exc:
            raise RpcHandlerError(
                "PENDING_INPUT_NOT_FOUND",
                "Pending input no longer exists",
                retryable=False,
                accepted=False,
            ) from exc
        except PendingInputConflictError as exc:
            raise RpcHandlerError(
                "PENDING_INPUT_CONFLICT",
                "Pending input changed before update",
                retryable=True,
                accepted=False,
            ) from exc

    async def reorder(self, params: dict[str, Any] | None) -> dict[str, Any]:
        key = self._key(params)
        raw_items = (params or {}).get("items")
        if not isinstance(raw_items, list):
            raise ValueError("params.items must contain 2-5 rows")
        items = []
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise ValueError(f"params.items[{index}] must be an object")
            pending_id = raw.get("pendingInputId", raw.get("pending_input_id"))
            if pending_id is not None and not isinstance(pending_id, str):
                raise ValueError("params.pendingInputId must be a string")
            revision = self._optional_revision(raw)
            if revision is None or revision < 1:
                raise ValueError(
                    f"params.items[{index}].expectedRevision must be a positive integer"
                )
            items.append(PendingInputRevision(pending_id or "", revision))
        try:
            return dict(await self._application.reorder(ReorderPendingInputs(key, tuple(items))))
        except PendingInputConflictError as exc:
            raise RpcHandlerError(
                "PENDING_INPUT_CONFLICT",
                "Pending inputs changed before reorder",
                retryable=True,
                accepted=False,
            ) from exc

    async def cancel(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params or {}
        command = CancelPendingInput(
            self._key(params),
            self._optional_string(raw, "pendingInputId", "pending_input_id") or "",
            self._optional_revision(raw),
        )
        try:
            return dict(await self._application.cancel(command))
        except PendingCancellationConflictError as exc:
            raise RpcHandlerError(
                "PENDING_INPUT_CONFLICT",
                "Pending input changed before cancellation",
                retryable=True,
                accepted=False,
            ) from exc

    @staticmethod
    def _key(params: dict[str, Any] | None) -> str:
        raw = params if isinstance(params, dict) else {}
        key = raw.get("key", raw.get("sessionKey"))
        if not isinstance(key, str) or not key.strip():
            raise ValueError("params.key is required")
        return key

    @staticmethod
    def _value_string(raw: dict[str, Any], *names: str) -> str | None:
        for name in names:
            if name in raw:
                value = raw[name]
                if value is None:
                    return None
                if not isinstance(value, str):
                    raise ValueError(f"params.{names[0]} must be a string")
                return value.strip() or None
        return None

    def _required_value(self, raw: dict[str, Any], *names: str) -> str:
        value = self._value_string(raw, *names)
        if value is None:
            raise ValueError(f"params.{names[0]} is required")
        if len(value) > 256:
            raise ValueError(f"params.{names[0]} must not exceed 256 characters")
        return value

    def _enqueue_command(self, raw: dict[str, Any]) -> EnqueuePendingInput:
        key = self._key(raw)
        message = raw.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("params.message must be a non-empty string")
        attachments = raw.get("attachments", [])
        if attachments is None:
            attachments = []
        if not isinstance(attachments, list):
            raise ValueError("params.attachments must be an array")
        params: dict[str, Any] = {
            "key": key,
            "message": message,
            "attachments": attachments,
            "queueMode": "followup",
            "_source": raw.get("_source"),
            "clientRequestId": self._required_value(raw, "clientRequestId", "client_request_id"),
            "clientMessageId": self._required_value(raw, "clientMessageId", "client_message_id"),
        }
        for aliases, name in (
            (("intent",), "intent"),
            (("workspaceId", "workspace_id"), "workspaceId"),
            (("collaborationMode", "collaboration_mode"), "collaborationMode"),
            (("initialRoutingMode", "initial_routing_mode"), "initialRoutingMode"),
            (("displayText", "display_text"), "displayText"),
        ):
            value = self._value_string(raw, *aliases)
            if value is not None:
                params[name] = value
        annotations = raw.get("promptAnnotationIds", raw.get("prompt_annotation_ids"))
        if annotations is not None:
            if not isinstance(annotations, list):
                raise ValueError("params.promptAnnotationIds must be an array")
            if any(not isinstance(item, str) or not item.strip() for item in annotations):
                raise ValueError("params.promptAnnotationIds must contain non-empty strings")
            params["promptAnnotationIds"] = [item.strip() for item in annotations]
        confirmed = raw.get("confirmedPlainText", raw.get("confirmed_plain_text", False))
        if not isinstance(confirmed, bool):
            raise ValueError("params.confirmedPlainText must be a boolean")
        turn = decode_admit_turn(
            params,
            surface="session",
            principal_role=self._principal_role,
        )
        return EnqueuePendingInput(
            turn=turn,
            pending_input_id=self._optional_string(raw, "pendingInputId", "pending_input_id") or "",
            confirmed_plain_text=confirmed,
            position=raw.get("position"),
        )

    async def enqueue(self, params: dict[str, Any] | None) -> dict[str, Any]:
        try:
            return dict(await self._application.enqueue(self._enqueue_command(params or {})))
        except (
            PendingQueueRejectedError,
            PendingEnqueueRejectedError,
            PendingMaterialRejectedError,
        ) as exc:
            raise self._queue_error(exc, operation="enqueue") from exc

    async def dispatch(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params or {}
        source = decode_turn_source(raw)
        command = DispatchPendingInput(
            self._key(params),
            self._optional_string(raw, "pendingInputId", "pending_input_id") or "",
            self._required_value(raw, "clientRequestId", "client_request_id"),
            self._value_string(raw, "requestFingerprint", "request_fingerprint") or "",
            source_scope_from_turn(source, self._principal_role),
        )
        try:
            return dict(await self._application.dispatch(command))
        except PendingQueueRejectedError as exc:
            raise self._queue_error(exc, operation="dispatch") from exc
        except PendingChatInputNotFoundError as exc:
            raise RpcHandlerError(
                "PENDING_INPUT_NOT_FOUND",
                "Pending input disappeared before dispatch",
                retryable=True,
                accepted=False,
            ) from exc
        except (PendingChatInputConflictError, AdmissionPendingInputConflictError) as exc:
            raise RpcHandlerError(
                "PENDING_INPUT_CONFLICT",
                "Pending input changed before dispatch",
                retryable=True,
                accepted=False,
            ) from exc
        except Exception as exc:
            mapped = map_admission_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    async def steer(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params or {}
        source = decode_turn_source(raw)
        revision = self._optional_revision(raw)
        if revision is None or revision < 1:
            raise ValueError("params.expectedRevision must be a positive integer")
        command = SteerPendingInput(
            session_key=self._key(params),
            pending_input_id=self._optional_string(raw, "pendingInputId", "pending_input_id") or "",
            client_request_id=self._required_value(raw, "clientRequestId", "client_request_id"),
            client_message_id=self._required_value(raw, "clientMessageId", "client_message_id"),
            request_fingerprint=self._value_string(raw, "requestFingerprint", "request_fingerprint")
            or "",
            expected_revision=revision,
            source_scope=source_scope_from_turn(source, self._principal_role),
            expected_turn_id=self._value_string(raw, "expected_turn_id", "expectedTurnId"),
            surface_id=self._value_string(raw, "surface_id", "surfaceId"),
            retry_message=raw.get("message"),
            is_web_source=source.is_web,
            default_surface_id=source.channel_id or f"{source.caller_kind}:{source.channel_kind}",
        )
        try:
            return dict(await self._application.steer(command))
        except PendingQueueRejectedError as exc:
            raise self._queue_error(exc, operation="steer") from exc
        except Exception as exc:
            mapped = map_steering_error(exc, is_owner=self._is_owner)
            if mapped is exc:
                raise
            raise mapped from exc

    @staticmethod
    def _queue_error(
        exc: PendingQueueRejectedError | PendingEnqueueRejectedError | PendingMaterialRejectedError,
        *,
        operation: str,
    ) -> RpcHandlerError:
        if isinstance(exc, PendingMaterialRejectedError):
            if exc.reason in {"expired", "restart-lost"}:
                return RpcHandlerError(
                    "ATTACHMENT_EXPIRED"
                    if exc.reason == "expired"
                    else "ATTACHMENT_LOST_IN_RESTART",
                    str(exc),
                    details={
                        "attachmentIndex": exc.attachment_index,
                        "fileUuid": exc.file_uuid,
                        "recovery": "reupload" if exc.recoverable else None,
                    },
                    retryable=exc.recoverable,
                    accepted=False,
                )
            codes = {
                "conflict": (
                    "PENDING_INPUT_CONFLICT",
                    "A pending input id was reused for different content",
                ),
                "corrupt": (
                    "PENDING_ATTACHMENT_RECOVERY_CORRUPT",
                    "Queued attachment recovery data is invalid; cancel and requeue it",
                ),
                "invalid": ("PENDING_ATTACHMENT_INVALID", str(exc)),
            }
            code, message = codes[exc.reason]
            return RpcHandlerError(code, message, retryable=False, accepted=False)
        if isinstance(exc, PendingEnqueueRejectedError):
            codes = {
                "full": ("PENDING_INPUTS_FULL", "This session already has five queued messages"),
                "cancelled": (
                    "PENDING_INPUT_CANCELLED",
                    "This queued message was already cancelled",
                ),
                "dispatched": (
                    "PENDING_INPUT_ALREADY_DISPATCHED",
                    "This queued message was already dispatched",
                ),
                "conflict": (
                    "PENDING_INPUT_CONFLICT",
                    "A pending input id was reused for different content",
                ),
            }
            code, message = codes[exc.reason]
            return RpcHandlerError(
                code,
                message,
                details={"maxPending": 5} if exc.reason == "full" else None,
                retryable=False,
                accepted=False,
            )
        codes = {
            "control-command": (
                "PENDING_CONTROL_COMMAND_UNSUPPORTED",
                "Client control commands cannot be staged for later dispatch",
            ),
            "registered-control-command": (
                "PENDING_CONTROL_COMMAND_UNSUPPORTED",
                "Registered client control commands cannot be staged for later dispatch",
            ),
            "display-mismatch": (
                "PENDING_DISPLAY_TEXT_MISMATCH",
                "Pending display text must match the provider message "
                "or an exact literal slash escape",
            ),
            "initial-routing": (
                "PENDING_INITIAL_ROUTING_UNSUPPORTED",
                "Send a new chat's initialRoutingMode with its first chat.send request.",
            ),
            "session-unavailable": (
                "PENDING_SESSION_UNAVAILABLE",
                "Queued messages require an existing durable session",
            ),
            "fingerprint-required": (
                "PENDING_INPUT_FINGERPRINT_REQUIRED",
                f"Pending input {operation} requires its staged fingerprint",
            ),
            "missing": ("PENDING_INPUT_NOT_FOUND", "Pending input no longer exists"),
            "fingerprint-conflict": (
                "PENDING_INPUT_CONFLICT",
                "Pending input fingerprint does not match its accepted turn",
            ),
            "dispatch-identity": (
                "PENDING_INPUT_CONFLICT",
                "Pending input dispatch identity does not match the staged row",
            ),
            "steer-identity": (
                "PENDING_INPUT_CONFLICT",
                "Pending input steer identity does not match the staged row",
            ),
        }
        code, message = codes[exc.reason]
        return RpcHandlerError(code, message, retryable=exc.retryable, accepted=False)
