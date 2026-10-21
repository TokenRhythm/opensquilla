"""Read durable turn acceptance without entering an admission or runtime path."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from opensquilla.application.turn_admission import AdmitTurn, SteerTurn
from opensquilla.gateway.adapters.turn_admission import (
    GatewayTurnAdmissionAdapter,
    webchat_session_key,
)
from opensquilla.gateway.admission_input import (
    decode_admit_turn,
    normalized_source_hint,
    source_scope_from_hint,
)
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc.registry import RpcHandlerError
from opensquilla.gateway.scopes import operator_scope_satisfies
from opensquilla.gateway.turn_steering import decode_steering_command
from opensquilla.session.keys import canonicalize_session_key
from opensquilla.session.storage import SessionStorage, bounded_interactive_storage_reads

TURN_RECEIPT_METHOD: Final = "turns.receipt.get"
TURN_RECEIPT_CAPABILITY: Final = "turns.receipt.read.v1"
TURN_RECEIPT_OPERATIONS = frozenset({
    "chat.send", "sessions.send", "sessions.steer.v2",
    "sessions.pending_inputs.dispatch", "sessions.pending_inputs.steer",
})
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


def can_read_turn_receipts(principal: Principal) -> bool:
    """Use existing shared-operator authority; source hints never grant read access."""
    return (
        principal.role == "operator"
        and (principal.authenticated or principal.is_owner)
        and principal.auth_state not in {"guest", "invalid"}
        and "guest.safe" not in principal.capabilities
        and operator_scope_satisfies("operator.read", principal.scopes)
    )


def _text(params: dict[str, Any], *names: str, required: bool = True) -> str | None:
    values = [params[name] for name in names if name in params]
    normalized: list[str | None] = []
    for value in values:
        if value is None and not required:
            normalized.append(None)
        elif isinstance(value, str) and value.strip() and len(value) <= 512:
            normalized.append(value.strip())
        else:
            raise ValueError(f"originalRequest.{names[0]} must be a non-empty string")
    if normalized and any(value != normalized[0] for value in normalized[1:]):
        raise ValueError(f"originalRequest aliases for {names[0]} must match")
    result = normalized[0] if normalized else None
    if result is None and required:
        raise ValueError(f"originalRequest.{names[0]} is required")
    return result


def _fingerprint(value: Any) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ValueError("requestFingerprint must be a previously known SHA-256 fingerprint")
    return value


def _conflict() -> RpcHandlerError:
    # This says nothing about whether the original mutation was accepted.
    return RpcHandlerError("IDEMPOTENCY_CONFLICT", "Receipt identity does not match the request")


@dataclass(frozen=True)
class TurnReceiptQuery:
    operation: str
    session_key: str
    source_scope: str
    client_request_id: str
    request_fingerprint: str
    pending_input_id: str | None = None
    client_message_id: str | None = None
    expected_turn_id: str | None = None
    surface_id: str | None = None

    @property
    def is_steer(self) -> bool:
        return self.operation in {"sessions.steer.v2", "sessions.pending_inputs.steer"}


def decode_turn_receipt_query(params: dict[str, Any], *, principal_role: str) -> TurnReceiptQuery:
    operation = params["operation"]
    if operation not in TURN_RECEIPT_OPERATIONS:
        raise ValueError("Unsupported original operation")
    original = params["originalRequest"]
    if not isinstance(original, dict):
        raise ValueError("originalRequest must be an object")
    request_id = _text(original, "clientRequestId", "client_request_id")
    assert request_id is not None
    if len(request_id) > 256:
        raise ValueError("clientRequestId must not exceed 256 characters")
    raw_key = _text(original, "key", "sessionKey", "session_key", required=False)
    if operation != "chat.send" and raw_key is None:
        raise ValueError("originalRequest.key is required")
    key = webchat_session_key(raw_key) if operation == "chat.send" else canonicalize_session_key(
        raw_key or ""
    )
    source = normalized_source_hint(original)
    scope = (
        f"web:webchat:{principal_role}"[:256] if operation == "chat.send"
        else source_scope_from_hint(source, principal_role)
    )
    known = _fingerprint(params["requestFingerprint"]) if "requestFingerprint" in params else None
    pending_id = message_id = target = surface = None
    if operation.startswith("sessions.pending_inputs."):
        pending_id = _text(original, "pendingInputId", "pending_input_id")
        fingerprint = _fingerprint(_text(original, "requestFingerprint", "request_fingerprint"))
    elif "message" in original:
        command: AdmitTurn | SteerTurn
        if operation == "sessions.steer.v2":
            command = decode_steering_command(original, key=key, principal_role=principal_role)
        elif operation == "chat.send":
            command = GatewayTurnAdmissionAdapter.decode_webchat_command(
                original, key, principal_role=principal_role,
            )
        else:
            command = decode_admit_turn(
                {**original, "key": key}, principal_role=principal_role, allow_receipt_replay=True,
            )
        fingerprint = command.request_fingerprint
        scope = command.source_scope
    elif known is not None:
        # No text or attachment body is needed after learning the authoritative hash.
        identity_fields = {
            "key", "sessionKey", "session_key", "clientRequestId", "client_request_id",
            "clientMessageId", "client_message_id", "expectedTurnId", "expected_turn_id",
            "surfaceId", "surface_id", "_source",
        }
        if set(original) - identity_fields:
            raise ValueError("Compact lookup accepts identity fields only")
        fingerprint = known
    else:
        raise ValueError("Original message or a known requestFingerprint is required")
    if operation == "sessions.steer.v2":
        scope = f"{source_scope_from_hint(source, principal_role)}:steer.v2"[:256]
    if known is not None and known != fingerprint:
        raise _conflict()
    if operation in {"sessions.steer.v2", "sessions.pending_inputs.steer"}:
        message_id = _text(original, "clientMessageId", "client_message_id")
        target = _text(original, "expectedTurnId", "expected_turn_id")
        surface = _text(original, "surfaceId", "surface_id", required=False)
        if surface is None and operation == "sessions.steer.v2":
            surface = str(source.get("channel_id") or (
                f"{source.get('caller_kind', 'rpc')}:{source.get('channel_kind', 'rpc')}"
            ))
    if any(value is not None and len(value) > 256 for value in (pending_id, message_id, target)):
        raise ValueError("Original request identifiers must not exceed 256 characters")
    return TurnReceiptQuery(
        operation, key, scope, request_id, fingerprint, pending_id, message_id, target, surface,
    )


async def read_turn_receipt(storage: SessionStorage, query: TurnReceiptQuery) -> dict[str, Any]:
    """Query receipt coordinates and small metadata only; a miss stays indeterminate."""
    missing: dict[str, Any] = {"status": "not_found", "accepted": None}
    with bounded_interactive_storage_reads():
        if query.pending_input_id is not None:
            pending = await storage.get_pending_chat_input_dispatch_receipt(query.pending_input_id)
            if pending is None:
                return missing
            if (
                pending.session_key != query.session_key
                or pending.source_scope != query.source_scope
                or pending.client_request_id != query.client_request_id
                or pending.request_fingerprint != query.request_fingerprint
                or (query.is_steer and pending.client_message_id != query.client_message_id)
            ):
                raise _conflict()
        accepted = await storage.get_turn_ingress_receipt(
            source_scope=query.source_scope,
            request_session_key=query.session_key,
            client_request_id=query.client_request_id,
        )
        if accepted is None:
            return missing
        receipt = accepted.receipt
        if receipt.request_fingerprint != query.request_fingerprint:
            raise _conflict()
        # Operators have shared session read access under the existing scope policy.
        # The accepted target comes exclusively from this receipt, including handoff.
        context = await storage.get_turn_receipt_context(receipt.session_id, receipt.message_id)
        if context is None and (query.is_steer or query.pending_input_id is not None):
            return missing
        context = context or {}
        if (context.get("intent") == "steer") != query.is_steer:
            raise _conflict()
        task = await storage.get_agent_task(receipt.task_id) if receipt.task_id else None
    details = task.details if task is not None and isinstance(task.details, dict) else {}
    epoch = details.get("session_epoch")
    if (details.get("session_id") != receipt.session_id
            or not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0):
        epoch = None
    result: dict[str, Any] = {
        "requestSessionKey": receipt.request_session_key,
        "sessionKey": receipt.accepted_session_key,
        "sessionId": receipt.session_id,
        "sessionEpoch": epoch,
        "clientRequestId": receipt.client_request_id,
        "messageId": receipt.message_id,
        "taskId": receipt.task_id,
        "taskStatus": str(task.status) if task is not None else None,
    }
    if query.is_steer:
        if (
            context.get("target_turn_id") != query.expected_turn_id
            or context.get("client_message_id") != query.client_message_id
            or (query.surface_id is not None and context.get("surface_id") != query.surface_id)
        ):
            raise _conflict()
        # Pending steering can inherit its surface from the already-consumed
        # queue row. The durable context retains that default after restart.
        surface_id = context.get("surface_id")
        if not isinstance(surface_id, str) or not surface_id:
            return missing
        steer: dict[str, Any] = {
            "status": "accepted", "accepted": True, "replayed": True,
            "key": receipt.accepted_session_key, "session_key": receipt.accepted_session_key,
            "session_id": receipt.session_id, "task_id": receipt.task_id,
            "turn_id": receipt.task_id, "client_request_id": receipt.client_request_id,
            "client_message_id": query.client_message_id, "user_message_id": receipt.message_id,
            "surface_id": surface_id, "disposition": context.get("disposition") or "steering",
            "revision": context.get("revision") or 1,
            # A lookup can never authorize a followup mutation on a failed steer.
            "fallback_safe": False,
        }
        for name in (
            "promoted_turn_id", "promoted_from_turn_id", "applied_iteration", "model_call_id",
            "failure_code", "retryable", "recovery",
        ):
            if context.get(name) is not None:
                steer[name] = context[name]
        if steer["disposition"] == "promoted" and "promoted_turn_id" not in steer:
            steer["promoted_turn_id"] = context.get("turn_id")
        result["steer"] = steer
    return {
        "status": "found", "accepted": True,
        "requestFingerprint": receipt.request_fingerprint, "receipt": result,
    }
