"""Stable, user-safe product errors for Artifact and Workbench surfaces.

The Artifact subsystem intentionally keeps detailed consistency diagnostics in
structured logs.  RPC callers receive only a stable recovery code, a generic
message, and an opaque correlation identifier.  This prevents implementation
concepts such as receipts, leases, revisions, and storage exceptions from
becoming product copy while retaining enough information for support.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import uuid4

import structlog

from opensquilla.gateway.rpc import RpcHandlerError

log = structlog.get_logger(__name__)


class ArtifactProductErrorCode(StrEnum):
    """Public recovery categories shared by Gateway and clients."""

    DOCUMENT_CHANGED = "DOCUMENT_CHANGED"
    EDIT_SESSION_RENEWAL_REQUIRED = "EDIT_SESSION_RENEWAL_REQUIRED"
    WRITE_BUSY = "WRITE_BUSY"
    MUTATION_NOT_APPLIED = "MUTATION_NOT_APPLIED"
    MUTATION_OUTCOME_PENDING = "MUTATION_OUTCOME_PENDING"
    DOCUMENT_UNAVAILABLE = "DOCUMENT_UNAVAILABLE"
    RESOURCE_UNSUPPORTED = "RESOURCE_UNSUPPORTED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    PREVIEW_CAPABILITY_EXPIRED = "PREVIEW_CAPABILITY_EXPIRED"
    PREVIEW_RENDERER_FAILED = "PREVIEW_RENDERER_FAILED"
    ANNOTATION_UNAVAILABLE = "ANNOTATION_UNAVAILABLE"
    ANNOTATION_BUSY = "ANNOTATION_BUSY"
    INVALID_REQUEST = "INVALID_REQUEST"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_SAFE_MESSAGES: dict[ArtifactProductErrorCode, str] = {
    ArtifactProductErrorCode.DOCUMENT_CHANGED: (
        "The page changed while this update was being prepared. Refresh and try again."
    ),
    ArtifactProductErrorCode.EDIT_SESSION_RENEWAL_REQUIRED: (
        "Editing is reconnecting. Your unsaved changes are still available."
    ),
    ArtifactProductErrorCode.WRITE_BUSY: (
        "The page is being updated. Wait a moment and try again."
    ),
    ArtifactProductErrorCode.MUTATION_NOT_APPLIED: (
        "The page was not updated. You can try again."
    ),
    ArtifactProductErrorCode.MUTATION_OUTCOME_PENDING: (
        "The update result cannot be confirmed yet. Open the page to check before retrying."
    ),
    ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE: (
        "This page is temporarily unavailable. Try again."
    ),
    ArtifactProductErrorCode.RESOURCE_UNSUPPORTED: (
        "This file cannot be edited here."
    ),
    ArtifactProductErrorCode.PERMISSION_DENIED: (
        "You do not have permission to update this page."
    ),
    ArtifactProductErrorCode.PREVIEW_CAPABILITY_EXPIRED: (
        "The preview needs to be reopened."
    ),
    ArtifactProductErrorCode.PREVIEW_RENDERER_FAILED: (
        "The preview could not be displayed. Try reopening it."
    ),
    ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE: (
        "This annotation is temporarily unavailable."
    ),
    ArtifactProductErrorCode.ANNOTATION_BUSY: (
        "Annotations are being updated. Wait a moment and try again."
    ),
    ArtifactProductErrorCode.INVALID_REQUEST: (
        "The request could not be completed. Check the input and try again."
    ),
    ArtifactProductErrorCode.INTERNAL_ERROR: (
        "The operation could not be completed. Try again."
    ),
}


# Additive compatibility for clients and persisted outcomes produced before
# the stable product vocabulary was introduced.  New RPCs must emit only the
# canonical values above.
_LEGACY_CODE_ALIASES: dict[str, ArtifactProductErrorCode] = {
    "ARTIFACT_REVISION_CHANGED": ArtifactProductErrorCode.DOCUMENT_CHANGED,
    "ARTIFACT_SOURCE_CHANGED": ArtifactProductErrorCode.DOCUMENT_CHANGED,
    "ARTIFACT_DOCUMENT_CONFLICT": ArtifactProductErrorCode.DOCUMENT_CHANGED,
    "ARTIFACT_CHANGE_NOT_HEAD": ArtifactProductErrorCode.DOCUMENT_CHANGED,
    "ARTIFACT_CONFLICT": ArtifactProductErrorCode.DOCUMENT_CHANGED,
    "ARTIFACT_PREVIEW_CHANGED": ArtifactProductErrorCode.DOCUMENT_CHANGED,
    "ARTIFACT_SELECTION_CHANGED": ArtifactProductErrorCode.DOCUMENT_CHANGED,
    "DOCUMENT_RESOURCE_CONFLICT": ArtifactProductErrorCode.DOCUMENT_CHANGED,
    "DOCUMENT_MUTATION_CONFLICT": ArtifactProductErrorCode.DOCUMENT_CHANGED,
    "WORKBENCH_CURSOR_STALE": ArtifactProductErrorCode.DOCUMENT_CHANGED,
    "ARTIFACT_EDIT_SESSION_EXPIRED": (
        ArtifactProductErrorCode.EDIT_SESSION_RENEWAL_REQUIRED
    ),
    "ARTIFACT_EDIT_SESSION_STALE": (
        ArtifactProductErrorCode.EDIT_SESSION_RENEWAL_REQUIRED
    ),
    "ARTIFACT_EDIT_SESSION_CONFLICT": (
        ArtifactProductErrorCode.EDIT_SESSION_RENEWAL_REQUIRED
    ),
    "ARTIFACT_WRITER_LEASE_CONFLICT": ArtifactProductErrorCode.WRITE_BUSY,
    "STORAGE_BUSY": ArtifactProductErrorCode.WRITE_BUSY,
    "ARTIFACT_CHANGE_NOT_APPLIED": ArtifactProductErrorCode.MUTATION_NOT_APPLIED,
    "ARTIFACT_MUTATION_CLEANUP_AMBIGUOUS": (
        ArtifactProductErrorCode.MUTATION_OUTCOME_PENDING
    ),
    "ARTIFACT_ANNOTATION_NOT_DRAFT": ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
    "ARTIFACT_FOCUS_UNAVAILABLE": ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
    "ARTIFACT_FOCUS_UNSUPPORTED": ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
    "ARTIFACT_SELECTION_UNAVAILABLE": ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
    "ARTIFACT_SELECTION_UNSUPPORTED": ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
    "ARTIFACT_SOURCE_ENCODING": ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
    "ARTIFACT_SOURCE_TOO_LARGE": ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
    "ARTIFACT_SOURCE_UNSUPPORTED": ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
    "DOCUMENT_IMPORT_FORMAT_UNSUPPORTED": ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
    "DOCUMENT_IMPORT_ENCODING_UNSUPPORTED": ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
    "DOCUMENT_IMPORT_SIZE_UNSUPPORTED": ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
    "DOCUMENT_IMPORT_HTML_INVALID": ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
    "DOCUMENT_BUNDLE_UNSUPPORTED": ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
    "DOCUMENT_PUBLISH_FORMAT_UNSUPPORTED": ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
    "WORKBENCH_PREVIEW_ENCODING_UNSUPPORTED": ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
    "WORKBENCH_PREVIEW_UNSUPPORTED": ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
    "INVALID_PARAMS": ArtifactProductErrorCode.INVALID_REQUEST,
    "BAD_REQUEST": ArtifactProductErrorCode.INVALID_REQUEST,
    "NOT_FOUND": ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
    "UNAVAILABLE": ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
    "UNAUTHORIZED": ArtifactProductErrorCode.PERMISSION_DENIED,
}


def canonical_artifact_product_code(value: object) -> ArtifactProductErrorCode:
    """Normalize a current or compatibility code without inspecting messages."""

    normalized = str(value or "").strip().upper()
    try:
        return ArtifactProductErrorCode(normalized)
    except ValueError:
        return _LEGACY_CODE_ALIASES.get(
            normalized,
            ArtifactProductErrorCode.INTERNAL_ERROR,
        )


def artifact_product_error(
    code: ArtifactProductErrorCode,
    *,
    retryable: bool = False,
    retry_after_ms: int | None = None,
    accepted: bool | None = False,
    reason_code: str | None = None,
    correlation_id: str | None = None,
) -> RpcHandlerError:
    """Build a safe wire error containing no raw exception or internal state."""

    details: dict[str, Any] = {}
    if reason_code:
        details["reasonCode"] = reason_code
    if correlation_id:
        details["correlationId"] = correlation_id
    return RpcHandlerError(
        code.value,
        _SAFE_MESSAGES[code],
        details=details or None,
        retryable=retryable,
        retry_after_ms=retry_after_ms,
        accepted=accepted,
    )


def logged_artifact_product_error(
    code: ArtifactProductErrorCode,
    exc: BaseException,
    *,
    operation: str,
    retryable: bool = False,
    retry_after_ms: int | None = None,
    accepted: bool | None = False,
    reason_code: str | None = None,
    **context: Any,
) -> RpcHandlerError:
    """Log diagnostics and return the corresponding safe public error."""

    correlation_id = uuid4().hex
    log.error(
        "artifact.product_operation_failed",
        correlation_id=correlation_id,
        product_code=code.value,
        operation=operation,
        error_type=type(exc).__name__,
        error=str(exc),
        exc_info=exc,
        **context,
    )
    return artifact_product_error(
        code,
        retryable=retryable,
        retry_after_ms=retry_after_ms,
        accepted=accepted,
        reason_code=reason_code,
        correlation_id=correlation_id,
    )


__all__ = [
    "ArtifactProductErrorCode",
    "artifact_product_error",
    "canonical_artifact_product_code",
    "logged_artifact_product_error",
]
