"""ApprovalCenter v4 Contract seam.

This module deliberately does not import ``rpc_approvals`` or the ApprovalQueue.
It only validates the existing v4 wire and projects the browser-safe approval
event fields onto a small transport-independent value.  The production Gateway
implementation remains the owner of side effects; this module is the typed
compatibility seam used by adapters and Contract tests.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from pydantic import ValidationError

from opensquilla.contracts.generated.v4.approval_events import (
    ApprovalEventCanonicalPayload,
    ApprovalEventLegacyPayload,
    ApprovalEventPayload,
)
from opensquilla.contracts.generated.v4.approval_events_metadata import (
    APPROVAL_EVENTS_EVENT,
    APPROVAL_EVENTS_EVENT_METADATA,
    APPROVAL_EVENTS_SCHEMA_VERSION,
)
from opensquilla.contracts.generated.v4.approval_extend import (
    ApprovalExtendRequestFrame,
    ApprovalExtendResponseFrame,
    ApprovalExtendResult,
)
from opensquilla.contracts.generated.v4.approval_extend_metadata import (
    EXEC_APPROVAL_EXTEND_METHOD,
)
from opensquilla.contracts.generated.v4.approval_resolve import (
    ApprovalResolveRequestFrame,
    ApprovalResolveResponseFrame,
    ApprovalResolveResult,
)
from opensquilla.contracts.generated.v4.approval_resolve_metadata import (
    EXEC_APPROVAL_RESOLVE_METHOD,
)
from opensquilla.contracts.generated.v4.approval_snapshot import (
    ExecApprovalSnapshotRequestFrame,
    ExecApprovalSnapshotResponseFrame,
    ExecApprovalSnapshotResult,
)
from opensquilla.contracts.generated.v4.approval_snapshot_metadata import (
    EXEC_APPROVAL_SNAPSHOT_METHOD,
)
from opensquilla.contracts.generated.v4.approval_status import (
    ApprovalStatusRequestFrame,
    ApprovalStatusResponseFrame,
    ApprovalStatusResult,
)
from opensquilla.contracts.generated.v4.approval_status_metadata import (
    EXEC_APPROVAL_STATUS_METHOD,
)

log = logging.getLogger(__name__)

_MISSING = object()
_PRIVATE_HTTP_FIELD_RE = re.compile(
    r"(?:params|token|secret|password|credential|authorization|fingerprint|review|claim)",
    re.IGNORECASE,
)
_PRIVATE_DISPLAY_FIELD_RE = re.compile(
    r"(?:authorization|cookie|credential|fingerprint|password|review.?action|secret|session.?(?:key|id)|token|claim)",
    re.IGNORECASE,
)
_INTERNAL_DISPLAY_FIELD_RE = re.compile(
    r"^(?:action|actions|choice|choices|params|policy|reviewer)$",
    re.IGNORECASE,
)
_REDACTION_OMIT = object()

# The plugin handlers intentionally share the exec Contract.  These aliases
# are authored here, at the compatibility seam, rather than copied into a
# page, a client, or the application implementation.
APPROVAL_METHOD_ALIASES: dict[str, str] = {
    EXEC_APPROVAL_SNAPSHOT_METHOD: EXEC_APPROVAL_SNAPSHOT_METHOD,
    EXEC_APPROVAL_STATUS_METHOD: EXEC_APPROVAL_STATUS_METHOD,
    "plugin.approval.status": EXEC_APPROVAL_STATUS_METHOD,
    EXEC_APPROVAL_RESOLVE_METHOD: EXEC_APPROVAL_RESOLVE_METHOD,
    "plugin.approval.resolve": EXEC_APPROVAL_RESOLVE_METHOD,
    EXEC_APPROVAL_EXTEND_METHOD: EXEC_APPROVAL_EXTEND_METHOD,
    "plugin.approval.extend": EXEC_APPROVAL_EXTEND_METHOD,
}

APPROVAL_EVENT_WIRE_NAMES = frozenset(
    name
    for name in APPROVAL_EVENTS_EVENT_METADATA.get("wireNames", ())
    if isinstance(name, str)
)


class ApprovalCenterContractError(ValueError):
    """Raised when a value cannot safely cross the approval Contract seam."""


@dataclass(frozen=True, slots=True)
class ApprovalStatusProjection:
    """Domain-safe status projection shared by status/resolve/extend routes."""

    id: str
    namespace: str
    pending: bool
    resolved: bool
    approved: bool
    resolution: str
    consumed: bool
    deadline: int | float | None
    found: bool | None = None
    resolution_in_progress: bool = False


@dataclass(frozen=True, slots=True)
class ApprovalEventProjection:
    """Redacted display projection delivered across the ApprovalCenter seam."""

    event: str
    approval_id: str
    namespace: str
    session_key: str | None
    tool_name: str | None
    command: str | None
    approval_kind: str | None
    agent: str | None
    args: Mapping[str, Any] | None
    warning: str | None
    display_kind: str | None
    display_target: str | None
    destructive: bool | None
    irreversible: bool | None
    backup_state: str | None
    created_at: int | float | None
    deadline: int | float | None
    approved: bool | None
    resolution: str | None
    stream_seq: int | None
    schema_version: int | None
    legacy: bool


_REQUEST_MODELS: dict[str, type[Any]] = {
    EXEC_APPROVAL_SNAPSHOT_METHOD: ExecApprovalSnapshotRequestFrame,
    EXEC_APPROVAL_STATUS_METHOD: ApprovalStatusRequestFrame,
    EXEC_APPROVAL_RESOLVE_METHOD: ApprovalResolveRequestFrame,
    EXEC_APPROVAL_EXTEND_METHOD: ApprovalExtendRequestFrame,
}
_RESPONSE_MODELS: dict[str, type[Any]] = {
    EXEC_APPROVAL_SNAPSHOT_METHOD: ExecApprovalSnapshotResponseFrame,
    EXEC_APPROVAL_STATUS_METHOD: ApprovalStatusResponseFrame,
    EXEC_APPROVAL_RESOLVE_METHOD: ApprovalResolveResponseFrame,
    EXEC_APPROVAL_EXTEND_METHOD: ApprovalExtendResponseFrame,
}
_RESULT_MODELS: dict[str, type[Any]] = {
    EXEC_APPROVAL_SNAPSHOT_METHOD: ExecApprovalSnapshotResult,
    EXEC_APPROVAL_STATUS_METHOD: ApprovalStatusResult,
    EXEC_APPROVAL_RESOLVE_METHOD: ApprovalResolveResult,
    EXEC_APPROVAL_EXTEND_METHOD: ApprovalExtendResult,
}


def _canonical_method(method: Any) -> str:
    if not isinstance(method, str) or method not in APPROVAL_METHOD_ALIASES:
        raise ApprovalCenterContractError(f"unsupported approval method: {method!r}")
    return APPROVAL_METHOD_ALIASES[method]


def _validation_errors(exc: ValidationError) -> tuple[dict[str, Any], ...]:
    return tuple(
        cast(
            list[dict[str, Any]],
            exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            ),
        )
    )


def approval_request_contract_errors(
    method: Any,
    params: Any = _MISSING,
    *,
    request_id: str = "contract-probe",
) -> tuple[dict[str, Any], ...]:
    """Observe request drift without changing historical Gateway errors."""

    try:
        canonical = _canonical_method(method)
        frame: dict[str, Any] = {
            "type": "req",
            "id": request_id,
            "method": method,
        }
        if params is not _MISSING:
            frame["params"] = params
        _REQUEST_MODELS[canonical].model_validate(frame)
    except ApprovalCenterContractError as exc:
        return ({"type": "contract", "message": str(exc)},)
    except ValidationError as exc:
        return _validation_errors(exc)
    return ()


def build_approval_request(
    method: str,
    params: Any = _MISSING,
    *,
    request_id: str = "contract-probe",
) -> dict[str, Any]:
    """Build and validate a v4 request frame, preserving aliases verbatim."""

    canonical = _canonical_method(method)
    frame: dict[str, Any] = {
        "type": "req",
        "id": request_id,
        "method": method,
    }
    if params is not _MISSING:
        frame["params"] = params
    try:
        _REQUEST_MODELS[canonical].model_validate(frame)
    except ValidationError as exc:
        raise ApprovalCenterContractError(
            f"{method} request violated its v4 Contract"
        ) from exc
    return frame


def validate_approval_response_frame(
    method: str,
    frame: Any,
) -> dict[str, Any]:
    """Validate a success or error response and return the original mapping."""

    canonical = _canonical_method(method)
    if not isinstance(frame, dict):
        raise ApprovalCenterContractError(f"{method} response must be a JSON object")
    try:
        _RESPONSE_MODELS[canonical].model_validate(frame)
    except ValidationError as exc:
        raise ApprovalCenterContractError(
            f"{method} response violated its v4 Contract"
        ) from exc
    return frame


def validate_approval_result(method: str, payload: Any) -> dict[str, Any]:
    """Validate a result payload while retaining unknown additive fields."""

    canonical = _canonical_method(method)
    if not isinstance(payload, dict):
        raise ApprovalCenterContractError(f"{method} result must be a JSON object")
    try:
        _RESULT_MODELS[canonical].model_validate(payload)
    except ValidationError as exc:
        raise ApprovalCenterContractError(
            f"{method} result violated its v4 Contract"
        ) from exc
    return payload


def _alias_value(value: Mapping[str, Any], *names: str) -> Any:
    found: list[tuple[str, Any]] = []
    for name in names:
        if name in value and value[name] is not None:
            found.append((name, value[name]))
    if not found:
        return None
    first = found[0][1]
    if any(candidate != first for _, candidate in found[1:]):
        raise ApprovalCenterContractError(
            f"{APPROVAL_EVENTS_EVENT} has conflicting aliases: "
            + ", ".join(name for name, _ in found)
        )
    return first


def _text_alias(
    value: Mapping[str, Any],
    *names: str,
    required: bool = False,
) -> str | None:
    candidate = _alias_value(value, *names)
    if candidate is None:
        return None
    if not isinstance(candidate, str):
        raise ApprovalCenterContractError(
            f"{APPROVAL_EVENTS_EVENT} {names[0]} must be a string"
        )
    stripped = candidate.strip()
    if required and not stripped:
        raise ApprovalCenterContractError(
            f"{APPROVAL_EVENTS_EVENT} {names[0]} must be a non-empty string"
        )
    # Empty display strings are part of the current producer's wire contract
    # (for example, warning and approval_kind on a resolved event).  Preserve
    # them instead of turning a valid payload into a contract failure.
    return stripped


def _number_alias(value: Mapping[str, Any], *names: str) -> int | float | None:
    candidate = _alias_value(value, *names)
    if candidate is None:
        return None
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise ApprovalCenterContractError(
            f"{APPROVAL_EVENTS_EVENT} {names[0]} must be a JSON number"
        )
    if isinstance(candidate, float) and not math.isfinite(candidate):
        raise ApprovalCenterContractError(
            f"{APPROVAL_EVENTS_EVENT} {names[0]} must be finite"
        )
    return candidate


def _integer_alias(value: Mapping[str, Any], *names: str) -> int | None:
    candidate = _number_alias(value, *names)
    if candidate is None:
        return None
    if isinstance(candidate, float) and not candidate.is_integer():
        raise ApprovalCenterContractError(
            f"{APPROVAL_EVENTS_EVENT} {names[0]} must be an integer"
        )
    normalized = int(candidate)
    if normalized < 0:
        raise ApprovalCenterContractError(
            f"{APPROVAL_EVENTS_EVENT} {names[0]} must be non-negative"
        )
    return normalized


def _redact_display_value(value: Any, *, depth: int = 0) -> Any:
    """Return a bounded, non-sensitive copy for domain display projections."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if depth >= 2:
        return _REDACTION_OMIT
    if isinstance(value, (list, tuple)):
        return [
            normalized
            for item in list(value)[:20]
            if (normalized := _redact_display_value(item, depth=depth + 1)) is not _REDACTION_OMIT
        ]
    if not isinstance(value, Mapping):
        return _REDACTION_OMIT
    safe: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        is_redacted_marker = isinstance(item, str) and item == "[REDACTED]"
        if (
            (_PRIVATE_DISPLAY_FIELD_RE.search(key_text) and not is_redacted_marker)
            or _INTERNAL_DISPLAY_FIELD_RE.fullmatch(key_text)
        ):
            continue
        normalized = _redact_display_value(item, depth=depth + 1)
        if normalized is not _REDACTION_OMIT:
            safe[key_text] = normalized
    return safe


def _redacted_display_mapping(value: Any) -> Mapping[str, Any] | None:
    normalized = _redact_display_value(value)
    return normalized if isinstance(normalized, dict) else None


def _event_model_for(value: Mapping[str, Any], *, allow_legacy: bool) -> type[Any]:
    if "schema_version" not in value or value.get("schema_version") is None:
        if not allow_legacy:
            raise ApprovalCenterContractError(
                f"{APPROVAL_EVENTS_EVENT} payload is missing schema_version"
            )
        return ApprovalEventLegacyPayload
    version = value.get("schema_version")
    valid = (
        type(version) is int and version == APPROVAL_EVENTS_SCHEMA_VERSION
    ) or (
        type(version) is float
        and math.isfinite(version)
        and version == float(APPROVAL_EVENTS_SCHEMA_VERSION)
    )
    if not valid:
        raise ApprovalCenterContractError(
            f"{APPROVAL_EVENTS_EVENT} schema_version must be integer "
            f"{APPROVAL_EVENTS_SCHEMA_VERSION}"
        )
    return ApprovalEventCanonicalPayload


def validate_approval_event_payload(
    payload: Any,
    *,
    allow_legacy: bool = True,
) -> dict[str, Any]:
    """Validate a redacted event payload without changing its JSON tree."""

    if not isinstance(payload, dict):
        raise ApprovalCenterContractError(
            f"{APPROVAL_EVENTS_EVENT} payload must be a JSON object"
        )
    if not any(
        isinstance(payload.get(name), str) and payload.get(name).strip()
        for name in ("approval_id", "approvalId")
    ):
        raise ApprovalCenterContractError(
            f"{APPROVAL_EVENTS_EVENT} payload requires approval_id or approvalId"
        )
    model_type = _event_model_for(payload, allow_legacy=allow_legacy)
    try:
        model_type.model_validate(payload)
        # Also exercise the generated union target.  The authored discriminator
        # above is intentional: datamodel-code-generator cannot encode the
        # object-level absence assertion for the legacy branch.
        ApprovalEventPayload.model_validate(payload)
    except ValidationError as exc:
        raise ApprovalCenterContractError(
            f"{APPROVAL_EVENTS_EVENT} payload violated its v4 Contract"
        ) from exc
    return payload


def canonicalize_approval_event(
    event: str,
    payload: Any,
    *,
    allow_legacy: bool = True,
) -> ApprovalEventProjection:
    """Project aliases onto a safe value while leaving the wire payload intact."""

    if event not in APPROVAL_EVENT_WIRE_NAMES:
        raise ApprovalCenterContractError(f"unsupported approval event: {event!r}")
    value = validate_approval_event_payload(payload, allow_legacy=allow_legacy)
    approval_id = _text_alias(value, "approval_id", "approvalId", required=True)
    namespace = _text_alias(value, "namespace", required=True) or (
        "plugin" if event.startswith("plugin.") else "exec"
    )
    if namespace not in {"exec", "plugin"}:
        raise ApprovalCenterContractError(
            f"{APPROVAL_EVENTS_EVENT} namespace must be exec or plugin"
        )
    expected_namespace = "plugin" if event.startswith("plugin.") else "exec"
    if namespace != expected_namespace:
        raise ApprovalCenterContractError(
            f"{APPROVAL_EVENTS_EVENT} namespace does not match event name"
        )
    args = value.get("args")
    if args is not None and not isinstance(args, dict):
        raise ApprovalCenterContractError(
            f"{APPROVAL_EVENTS_EVENT} args must be an object or null"
        )
    version_value = value.get("schema_version")
    schema_version = (
        APPROVAL_EVENTS_SCHEMA_VERSION
        if version_value is not None
        else None
    )
    return ApprovalEventProjection(
        event=event,
        approval_id=cast(str, approval_id),
        namespace=namespace,
        session_key=_text_alias(value, "session_key", "sessionKey"),
        tool_name=_text_alias(value, "tool_name", "toolName"),
        command=_text_alias(value, "command"),
        approval_kind=_text_alias(value, "approval_kind", "approvalKind"),
        agent=_text_alias(value, "agent"),
        args=_redacted_display_mapping(args),
        warning=_text_alias(value, "warning"),
        display_kind=_text_alias(value, "display_kind", "displayKind"),
        display_target=_text_alias(value, "display_target", "displayTarget"),
        destructive=cast(bool | None, value.get("destructive")),
        irreversible=cast(bool | None, value.get("irreversible")),
        backup_state=_text_alias(value, "backup_state", "backupState"),
        created_at=_number_alias(value, "created_at", "createdAt"),
        deadline=_number_alias(value, "deadline"),
        approved=cast(bool | None, value.get("approved")),
        resolution=_text_alias(value, "resolution"),
        stream_seq=_integer_alias(value, "stream_seq", "streamSeq"),
        schema_version=schema_version,
        legacy=version_value is None,
    )


def observe_approval_event(
    event: str,
    payload: Any,
    *,
    source: str,
    allow_legacy: bool = True,
) -> Any:
    """Fail open for best-effort notifications while recording a diagnostic."""

    try:
        return canonicalize_approval_event(
            event,
            payload,
            allow_legacy=allow_legacy,
        )
    except ApprovalCenterContractError as exc:
        try:
            log.warning(
                "approval_center.contract_violation event=%s source=%s error_type=%s",
                event,
                source,
                type(exc).__name__,
            )
        except Exception:
            pass
        return payload


def project_approval_status(
    method: str,
    payload: Any,
    *,
    namespace: str | None = None,
) -> ApprovalStatusProjection:
    """Hide wire naming and preserve status semantics for the domain Module."""

    value = validate_approval_result(method, payload)
    expected_namespace = namespace or (
        "plugin" if method.startswith("plugin.") else "exec"
    )
    resolved_namespace = str(value.get("namespace") or expected_namespace)
    if resolved_namespace not in {"exec", "plugin"}:
        raise ApprovalCenterContractError("approval status namespace is invalid")
    if value.get("namespace") is not None and resolved_namespace != expected_namespace:
        raise ApprovalCenterContractError(
            "approval status namespace does not match the requested method"
        )
    found = value.get("found") if "found" in value else None
    return ApprovalStatusProjection(
        id=str(value.get("id") or ""),
        namespace=resolved_namespace,
        pending=bool(value.get("pending")),
        resolved=bool(value.get("resolved")),
        approved=bool(value.get("approved")),
        resolution=str(value.get("resolution") or ""),
        consumed=bool(value.get("consumed")),
        deadline=value.get("deadline"),
        found=cast(bool | None, found),
        resolution_in_progress=bool(value.get("resolutionInProgress")),
    )


def validate_approval_http_snapshot(payload: Any) -> dict[str, Any]:
    """Validate the safe companion HTTP projection without importing Gateway."""

    if not isinstance(payload, dict):
        raise ApprovalCenterContractError("/api/approvals snapshot must be an object")
    snapshot_mode = payload.get("mode")
    if not isinstance(snapshot_mode, str) or snapshot_mode not in {
        "prompt",
        "auto-approve",
        "auto-deny",
    }:
        raise ApprovalCenterContractError("/api/approvals snapshot has invalid mode")
    pending = payload.get("pending")
    if not isinstance(pending, list):
        raise ApprovalCenterContractError("/api/approvals snapshot pending must be a list")
    for item in pending:
        if not isinstance(item, dict):
            raise ApprovalCenterContractError("approval snapshot item must be an object")
        if not isinstance(item.get("id"), str) or not item["id"]:
            raise ApprovalCenterContractError("approval snapshot item id is required")
        # Older HTTP projections did not include namespace; the WebUI adapter
        # treats that shape as the exec namespace for compatibility. Keep the
        # Python validator aligned instead of making generated clients stricter
        # than the live v4 endpoint.
        item_namespace = item.get("namespace")
        if item_namespace is not None and (
            not isinstance(item_namespace, str)
            or item_namespace not in {"exec", "plugin"}
        ):
            raise ApprovalCenterContractError("approval snapshot namespace is invalid")
        # Internal queue records must never cross the HTTP projection.  Keep
        # this check key-based so camel/snake legacy spellings cannot smuggle
        # claim tokens or review metadata through an otherwise open projection.
        if any(_PRIVATE_HTTP_FIELD_RE.search(str(key)) for key in item):
            raise ApprovalCenterContractError(
                "approval snapshot contains a non-display field"
            )
        for key in (
            "toolName",
            "sessionKey",
            "agent",
            "command",
            "warning",
            "approvalKind",
            "actionKind",
            "displayKind",
            "displayTarget",
            "backupState",
            "mode",
        ):
            if key in item and not isinstance(item[key], str):
                raise ApprovalCenterContractError(
                    f"approval snapshot {key} must be a string"
                )
        for key in ("created_at", "createdAt", "deadline"):
            if key in item and item[key] is not None:
                _value = item[key]
                if (
                    isinstance(_value, bool)
                    or not isinstance(_value, (int, float))
                    or (isinstance(_value, float) and not math.isfinite(_value))
                ):
                    raise ApprovalCenterContractError(
                        f"approval snapshot {key} must be a finite number or null"
                    )
        if (
            item.get("created_at") is not None
            and item.get("createdAt") is not None
            and item["created_at"] != item["createdAt"]
        ):
            raise ApprovalCenterContractError(
                "approval snapshot created_at and createdAt aliases conflict"
            )
        for key in ("destructive", "irreversible"):
            if key in item and not isinstance(item[key], bool):
                raise ApprovalCenterContractError(
                    f"approval snapshot {key} must be a boolean"
                )
        if "args" in item and item["args"] is not None and not isinstance(item["args"], dict):
            raise ApprovalCenterContractError(
                "approval snapshot args must be an object or null"
            )
    for key in ("allowPatterns", "denyPatterns"):
        if key in payload and (
            not isinstance(payload[key], list)
            or any(not isinstance(value, str) for value in payload[key])
        ):
            raise ApprovalCenterContractError(
                f"approval snapshot {key} must be a list of strings"
            )
    return payload


__all__ = [
    "APPROVAL_EVENT_WIRE_NAMES",
    "APPROVAL_EVENTS_EVENT",
    "APPROVAL_METHOD_ALIASES",
    "ApprovalCenterContractError",
    "ApprovalEventProjection",
    "ApprovalStatusProjection",
    "approval_request_contract_errors",
    "build_approval_request",
    "canonicalize_approval_event",
    "observe_approval_event",
    "project_approval_status",
    "validate_approval_event_payload",
    "validate_approval_http_snapshot",
    "validate_approval_response_frame",
    "validate_approval_result",
]
