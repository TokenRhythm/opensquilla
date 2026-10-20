"""Read durable turn acceptance without entering an admission or runtime path."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from opensquilla.application.turn_admission import AdmitTurn, SteerTurn
from opensquilla.contracts.turn_request_scope import resolvable_webchat_scope
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
from opensquilla.persistence.turn_request_resolution import (
    parse_turn_receipt_epoch,
    request_binds_epoch,
)
from opensquilla.session.keys import canonicalize_session_key
from opensquilla.session.storage import (
    SessionStorage,
    TurnIngressConflictError,
    bounded_interactive_storage_reads,
)

TURN_RECEIPT_METHOD: Final = "turns.receipt.get"
TURN_RECEIPT_CAPABILITY: Final = "turns.receipt.read.v1"
TURN_RECEIPT_RESOLVE_METHOD: Final = "turns.receipt.resolve"
TURN_RECEIPT_OPERATIONS = frozenset({
    "chat.send", "chat.send.v2", "sessions.send", "sessions.steer.v2",
    "sessions.pending_inputs.dispatch", "sessions.pending_inputs.steer",
})
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


def current_turn_receipt_epoch() -> str | None:
    # Import lazily: boot composes the RPC modules. This value survives socket
    # reconnects but cannot be rolled back by restoring a profile/database.
    from opensquilla.gateway.boot import get_gateway_turn_receipt_epochs

    policy = get_gateway_turn_receipt_epochs()
    return policy.current_epoch() if policy is not None else None


def is_current_turn_receipt_epoch(request_epoch: object) -> bool:
    current = parse_turn_receipt_epoch(current_turn_receipt_epoch())
    request = parse_turn_receipt_epoch(request_epoch)
    return (
        current is not None and request is not None and current.boot_id == request.boot_id
        and request.issued_hour <= current.issued_hour < request.expires_hour
    )


def can_read_turn_receipts(principal: Principal) -> bool:
    """Use existing shared-operator authority; source hints never grant read access."""
    return (
        principal.role == "operator"
        and (principal.authenticated or principal.is_owner)
        and principal.auth_state not in {"guest", "invalid"}
        and "guest.safe" not in principal.capabilities
        and operator_scope_satisfies("operator.read", principal.scopes)
    )


def can_resolve_turn_receipts(principal: Principal) -> bool:
    return can_read_turn_receipts(principal) and operator_scope_satisfies(
        "operator.write", principal.scopes,
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
    acceptance_epoch: str | None = None

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
    webchat = operation in {"chat.send", "chat.send.v2"}
    if not webchat and raw_key is None:
        raise ValueError("originalRequest.key is required")
    key = webchat_session_key(raw_key) if webchat else canonicalize_session_key(
        raw_key or ""
    )
    source = normalized_source_hint(original)
    scope = (
        f"web:webchat:{principal_role}"[:256] if webchat
        else source_scope_from_hint(source, principal_role)
    )
    if operation == "chat.send.v2":
        scope = resolvable_webchat_scope(principal_role)
    known = _fingerprint(params["requestFingerprint"]) if "requestFingerprint" in params else None
    pending_id = message_id = target = surface = None
    if operation.startswith("sessions.pending_inputs."):
        pending_id = _text(original, "pendingInputId", "pending_input_id")
        fingerprint = _fingerprint(_text(original, "requestFingerprint", "request_fingerprint"))
    elif "message" in original:
        command: AdmitTurn | SteerTurn
        if operation == "sessions.steer.v2":
            command = decode_steering_command(original, key=key, principal_role=principal_role)
        elif webchat:
            command = GatewayTurnAdmissionAdapter.decode_webchat_command(
                original, key, principal_role=principal_role,
                resolvable=operation == "chat.send.v2",
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
            "surfaceId", "surface_id", "_source", "acceptanceEpoch",
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
        _text(original, "acceptanceEpoch", required=False) if operation == "chat.send.v2" else None,
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
            if query.operation == "chat.send.v2":
                try:
                    state = await storage.get_turn_request_resolution(
                        source_scope=query.source_scope, request_session_key=query.session_key,
                        client_request_id=query.client_request_id,
                        request_fingerprint=query.request_fingerprint,
                    )
                except TurnIngressConflictError as exc:
                    raise _conflict() from exc
                if state == "retired":
                    return _terminal_resolution(query, "retired")
                if state == "accepted":
                    # Acceptance can commit between the two reads. Prefer its
                    # receipt before concluding that session deletion removed it.
                    accepted = await storage.get_turn_ingress_receipt(
                        source_scope=query.source_scope,
                        request_session_key=query.session_key,
                        client_request_id=query.client_request_id,
                    )
                    if accepted is None:
                        return _terminal_resolution(query, "accepted_unavailable")
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


def _terminal_resolution(query: TurnReceiptQuery, status: str) -> dict[str, Any]:
    return {
        "status": status, "accepted": status == "accepted_unavailable",
        "requestFingerprint": query.request_fingerprint,
        "clientRequestId": query.client_request_id, "requestSessionKey": query.session_key,
    }


async def resolve_turn_receipt(storage: SessionStorage, query: TurnReceiptQuery) -> dict[str, Any]:
    if query.operation != "chat.send.v2":
        raise ValueError("Only chat.send.v2 supports request retirement")
    if (
        not is_current_turn_receipt_epoch(query.acceptance_epoch)
        or not request_binds_epoch(query.client_request_id, query.acceptance_epoch)
    ):
        # A restored backup can erase acceptance evidence. Across process
        # generations only an existing terminal record can prove the outcome.
        return await read_turn_receipt(storage, query)
    try:
        status = await storage.resolve_turn_request_attempt(
            source_scope=query.source_scope, request_session_key=query.session_key,
            client_request_id=query.client_request_id,
            request_fingerprint=query.request_fingerprint,
        )
    except TurnIngressConflictError as exc:
        raise _conflict() from exc
    if status == "found":
        # Deletion after the transaction remains positive through the marker.
        return await read_turn_receipt(storage, query)
    if status == "not_found":
        return {"status": "not_found", "accepted": None}
    return _terminal_resolution(query, status)


async def replay_previous_epoch_send(
    storage: SessionStorage, query: TurnReceiptQuery,
) -> dict[str, Any]:
    """Fence delayed sends before admission preparation or any storage mutation."""
    result = await read_turn_receipt(storage, query)
    if result["status"] == "found":
        receipt = result["receipt"]
        return {
            "ok": True, "status": "accepted", "replayed": True,
            "sessionKey": receipt["sessionKey"], "session_id": receipt["sessionId"],
            "message_id": receipt["messageId"], "task_id": receipt["taskId"],
            "task_status": receipt["taskStatus"], "client_request_id": query.client_request_id,
        }
    if result["status"] == "accepted_unavailable":
        raise RpcHandlerError(
            "ACCEPTED_RECEIPT_UNAVAILABLE",
            "This request was accepted; its session is unavailable.",
            accepted=True, details={"receiptUnavailable": True},
        )
    if result["status"] == "retired":
        raise RpcHandlerError(
            "REQUEST_RETIRED", "This request was not accepted and cannot be sent again.",
            accepted=False, details={"requestRetired": True},
        )
    raise RpcHandlerError(
        "UNAVAILABLE", "The outcome of this earlier Gateway request is unknown.",
        accepted=None, retryable=False,
    )
