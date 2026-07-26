"""Stable request identities for durable turn acceptance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any

from opensquilla.session.keys import canonicalize_session_key
from opensquilla.session.storage import TurnAcceptanceResult


@dataclass(frozen=True)
class TurnRequestIdentity:
    source_scope: str
    request_session_key: str
    client_request_id: str
    request_fingerprint: str


_FINGERPRINT_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("message", ("message",)),
    ("display_text", ("displayText", "display_text")),
    ("attachments", ("attachments",)),
    ("intent", ("intent",)),
    ("fork_before_message_id", ("forkBeforeMessageId", "fork_before_message_id")),
    ("queue_mode", ("queueMode", "queue_mode")),
    ("no_memory_capture", ("noMemoryCapture", "no_memory_capture")),
    ("input_provenance", ("inputProvenance", "input_provenance")),
    (
        "input_provenance_kind",
        ("inputProvenanceKind", "input_provenance_kind", "provenance_kind"),
    ),
    ("run_kind", ("runKind", "run_kind")),
)


def _canonical_fingerprint_payload(params: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for canonical_name, aliases in _FINGERPRINT_FIELDS:
        for alias in aliases:
            if alias in params:
                payload[canonical_name] = params[alias]
                break
    return payload


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "$bytes_sha256": hashlib.sha256(value).hexdigest(),
            "$bytes_len": len(value),
        }
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    raise TypeError(f"Unsupported request fingerprint value: {type(value).__name__}")


def request_fingerprint(params: dict[str, Any]) -> str:
    """Hash stable logical request fields without retaining user content."""

    encoded = json.dumps(
        _canonical_fingerprint_payload(params),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def request_identity(
    params: dict[str, Any],
    *,
    request_session_key: str,
    source_scope: str,
    fingerprint_params: dict[str, Any] | None = None,
) -> TurnRequestIdentity:
    raw_request_id = params.get("clientRequestId", params.get("client_request_id"))
    if raw_request_id is None:
        client_request_id = str(uuid.uuid4())
    elif not isinstance(raw_request_id, str) or not raw_request_id.strip():
        raise ValueError("clientRequestId must be a non-empty string")
    else:
        client_request_id = raw_request_id.strip()
    if len(client_request_id) > 256:
        raise ValueError("clientRequestId must not exceed 256 characters")
    return TurnRequestIdentity(
        source_scope=source_scope,
        request_session_key=canonicalize_session_key(request_session_key),
        client_request_id=client_request_id,
        request_fingerprint=request_fingerprint(
            params if fingerprint_params is None else fingerprint_params
        ),
    )


def accepted_turn_payload(
    result: TurnAcceptanceResult,
    *,
    client_request_id: str,
) -> dict[str, Any]:
    receipt = result.receipt
    payload = {
        "status": "accepted",
        "accepted": True,
        "key": receipt.accepted_session_key,
        "sessionKey": receipt.accepted_session_key,
        "session_id": receipt.session_id,
        "message_id": receipt.message_id,
        "task_id": receipt.task_id,
        "client_request_id": client_request_id,
        "clientRequestId": client_request_id,
        "replayed": result.replayed,
    }
    if result.task_status is not None:
        task_status = getattr(result.task_status, "value", result.task_status)
        payload["task_status"] = task_status
        payload["taskStatus"] = task_status
    return payload


async def complete_durable_ingress[T](awaitable: Awaitable[T]) -> T:
    """Finish an ingress commit/activation pair even if its caller is cancelled.

    Once queue admission has been reserved, cancellation must not split the
    durable acceptance transaction from runtime activation.  The inner task is
    shielded and repeated cancellation requests are deferred until that small
    critical section settles.  Returning its result intentionally consumes the
    caller cancellation: a disconnected transport may drop the response, while
    its stable request id makes a later replay safe.
    """

    task = asyncio.ensure_future(awaitable)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
