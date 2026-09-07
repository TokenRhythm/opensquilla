"""Decode retained turn wire aliases without exposing packets to Applications."""

from __future__ import annotations

from typing import Any, cast

from opensquilla.application.turn_admission import AdmitTurn, InitialRoutingMode, PendingInputGuard
from opensquilla.application.turn_input import (
    DocumentTurnContext,
    IncomingTurnSource,
    MemoryCapturePolicy,
)
from opensquilla.gateway.turn_ingress import request_identity
from opensquilla.session.keys import canonicalize_session_key


def _optional_string(params: dict[str, Any], *names: str) -> str | None:
    for name in names:
        if name not in params:
            continue
        value = params[name]
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"params.{name} must be a string")
        return value.strip() or None
    return None


def normalized_source_hint(params: dict[str, Any]) -> dict[str, Any]:
    hint = params.get("_source")
    source = dict(hint) if isinstance(hint, dict) else {}
    caller = str(source.get("caller_kind") or source.get("callerKind") or "").strip().lower()
    channel = str(source.get("channel_kind") or source.get("channelKind") or "").strip().lower()
    if caller:
        source.setdefault("caller_kind", caller)
    if channel:
        source.setdefault("channel_kind", channel)
    if caller != "cli" and channel != "cli":
        source.setdefault("caller_kind", "web")
        source.setdefault("channel_kind", "web")
    return source


def source_scope_from_hint(source: dict[str, Any], principal_role: str) -> str:
    caller = str(source.get("caller_kind") or "rpc").strip().lower()
    channel = str(source.get("channel_kind") or caller).strip().lower()
    return f"{caller}:{channel}:{principal_role or 'operator'}"[:256]


def is_web_source_hint(source: dict[str, Any]) -> bool:
    normalized: dict[str, str] = {}
    for canonical, aliases in (
        ("caller_kind", ("caller_kind", "callerKind")),
        ("channel_kind", ("channel_kind", "channelKind")),
        ("source_kind", ("source_kind", "sourceKind")),
    ):
        for alias in aliases:
            value = source.get(alias)
            if isinstance(value, str):
                normalized[canonical] = value.strip().lower()
                break
    return (
        normalized.get("caller_kind") == "web"
        or normalized.get("channel_kind") in {"web", "webchat"}
        or normalized.get("source_kind") == "webui"
    )


def decode_turn_source(
    params: dict[str, Any], *, principal_role: str = "operator"
) -> IncomingTurnSource:
    return _source(normalized_source_hint(params))


def source_scope_from_turn(source: IncomingTurnSource, principal_role: str) -> str:
    return source_scope_from_hint(source_hint_from_turn(source), principal_role)


def source_hint_from_turn(source: IncomingTurnSource) -> dict[str, Any]:
    result: dict[str, Any] = {
        "caller_kind": source.caller_kind,
        "channel_kind": source.channel_kind,
    }
    for name in (
        "channel_id",
        "sender_id",
        "source_kind",
        "source_name",
        "elevated",
        "client_message_id",
        "surface_id",
    ):
        value = getattr(source, name)
        if value is not None:
            result[name] = value
    if source.run_mode is not None:
        result["runMode"] = source.run_mode
    return result


def _source(source: dict[str, Any]) -> IncomingTurnSource:
    def text(name: str) -> str | None:
        value = source.get(name)
        return value if isinstance(value, str) else None

    caller = text("caller_kind") or ""
    channel = text("channel_kind") or ""
    source_kind = text("source_kind")
    mode = source.get("runMode") or source.get("run_mode")
    return IncomingTurnSource(
        caller_kind=caller,
        channel_kind=channel,
        channel_id=text("channel_id"),
        sender_id=text("sender_id"),
        source_kind=source_kind,
        source_name=text("source_name"),
        elevated=text("elevated"),
        run_mode=mode if isinstance(mode, str) else None,
        is_web=is_web_source_hint(source),
        client_message_id=_optional_string(source, "client_message_id", "clientMessageId"),
        surface_id=_optional_string(source, "surface_id", "surfaceId"),
    )


def _capture(params: dict[str, Any]) -> MemoryCapturePolicy:
    source = params.get("_source")
    source = source if isinstance(source, dict) else {}

    def boolean(value: object) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off", ""}:
                return False
        return bool(value)

    no_capture = boolean(params.get("no_memory_capture", params.get("noMemoryCapture")))
    if no_capture is None:
        no_capture = boolean(source.get("no_memory_capture", source.get("noMemoryCapture")))
    provenance = next(
        (
            dict(value)
            for value in (
                params.get("input_provenance"),
                params.get("inputProvenance"),
                source.get("input_provenance"),
                source.get("inputProvenance"),
            )
            if isinstance(value, dict)
        ),
        None,
    )
    kind = (
        params.get("input_provenance_kind")
        or params.get("inputProvenanceKind")
        or params.get("provenance_kind")
        or source.get("input_provenance_kind")
        or source.get("inputProvenanceKind")
        or source.get("provenance_kind")
    )
    if provenance is None and kind:
        provenance = {"kind": str(kind)}
    elif provenance is not None and "kind" not in provenance and kind:
        provenance["kind"] = str(kind)
    # Public run-kind labels never grant an internal execution role.
    return MemoryCapturePolicy(bool(no_capture), provenance)


def _document_context(params: dict[str, Any]) -> DocumentTurnContext | None:
    if (
        "documentContext" in params
        and "document_context" in params
        and params["documentContext"] != params["document_context"]
    ):
        raise ValueError("Conflicting documentContext aliases")
    value = params.get("documentContext", params.get("document_context"))
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("params.documentContext must be an object")
    if not set(value) <= {"documentId", "document_id", "headRevisionId", "head_revision_id"}:
        raise ValueError("params.documentContext accepts only documentId and headRevisionId")
    for camel, snake in (("documentId", "document_id"), ("headRevisionId", "head_revision_id")):
        if camel in value and snake in value and value[camel] != value[snake]:
            raise ValueError(f"Conflicting {camel} aliases")
    document = value.get("documentId", value.get("document_id"))
    head = value.get("headRevisionId", value.get("head_revision_id"))
    if (
        not isinstance(document, str)
        or not document.strip()
        or not isinstance(head, str)
        or not head.strip()
    ):
        raise ValueError("params.documentContext requires non-empty documentId and headRevisionId")
    return DocumentTurnContext(document.strip(), head.strip())


def decode_admit_turn(
    params: dict[str, Any],
    *,
    surface: str = "session",
    principal_role: str = "operator",
    connection_id: str = "",
    pending_input: PendingInputGuard | None = None,
    fingerprint_params: dict[str, Any] | None = None,
) -> AdmitTurn:
    if "message" not in params:
        raise ValueError("params.message is required")
    message = params["message"]
    if not isinstance(message, str):
        raise ValueError("message must be a string")
    key = canonicalize_session_key(params["key"])
    source = normalized_source_hint(params)
    raw_ids = params.get("promptAnnotationIds", params.get("prompt_annotation_ids"))
    ids: tuple[str, ...] = ()
    if raw_ids is not None:
        if not isinstance(raw_ids, list):
            raise ValueError("params.promptAnnotationIds must be an array")
        if len(raw_ids) > 16:
            raise ValueError("params.promptAnnotationIds supports at most 16 items")
        if any(not isinstance(item, str) or not item.strip() for item in raw_ids):
            raise ValueError("params.promptAnnotationIds must contain non-empty strings")
        ids = tuple(item.strip() for item in raw_ids)
        if len(set(ids)) != len(ids):
            raise ValueError("params.promptAnnotationIds must contain unique ids")
    document = _document_context(params)
    attachments = params.get("attachments", [])
    attachments = attachments if isinstance(attachments, list) else []
    # The durable receipt identifies original material, not the shared guarded
    # text shown for every large paste. Application normalization runs later.
    fingerprint = dict(fingerprint_params or params)
    if raw_ids is not None:
        fingerprint.pop("prompt_annotation_ids", None)
        fingerprint["promptAnnotationIds"] = list(ids)
    if document is not None:
        fingerprint.pop("document_context", None)
        fingerprint["documentContext"] = {
            "documentId": document.document_id,
            "headRevisionId": document.head_revision_id,
        }
    identity = request_identity(
        params,
        request_session_key=key,
        source_scope=source_scope_from_hint(source, principal_role),
        fingerprint_params=fingerprint,
    )
    display = params.get("displayText")
    routing = _optional_string(params, "initialRoutingMode", "initial_routing_mode")
    if routing is not None and routing not in {"direct", "router", "ensemble"}:
        raise ValueError("initialRoutingMode must be direct, router, or ensemble")
    return AdmitTurn(
        session_key=key,
        message=message,
        surface="webchat" if surface == "webchat" else "session",
        source=_source(source),
        capture=_capture(params),
        client_request_id=identity.client_request_id,
        request_fingerprint=identity.request_fingerprint,
        source_scope=identity.source_scope,
        explicit_request_id="clientRequestId" in params or "client_request_id" in params,
        client_message_id=(
            _optional_string(params, "client_message_id", "clientMessageId")
            or _optional_string(source, "client_message_id", "clientMessageId")
        ),
        surface_id=(
            _optional_string(params, "surface_id", "surfaceId")
            or _optional_string(source, "surface_id", "surfaceId")
        ),
        attachments=tuple(attachments),
        intent=params.get("intent", "continue"),
        intent_was_provided=params.get("intent") is not None,
        fork_before_message_id=_optional_string(
            params, "forkBeforeMessageId", "fork_before_message_id"
        ),
        workspace_id=params.get("workspaceId", params.get("workspace_id")),
        prompt_annotation_ids=ids,
        document_context=document,
        display_text=display if isinstance(display, str) else None,
        queue_mode=params.get("queueMode") or params.get("queue_mode"),
        initial_collaboration_mode=params.get(
            "collaborationMode", params.get("collaboration_mode")
        ),
        initial_routing_mode=cast(InitialRoutingMode | None, routing),
        pending_input=pending_input,
    )
