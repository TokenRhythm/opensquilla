"""Typed Workbench resources plus explicit copy-import and immutable publish RPCs."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactConflictError,
    ArtifactKind,
    ArtifactSessionService,
    ArtifactValidationError,
    Document,
    DocumentImportAttempt,
    DocumentImportMode,
    DocumentImportResult,
    DocumentPublication,
    DocumentPublishResult,
    DocumentSourceBinding,
    DocumentSourceType,
    MutationAttemptStatus,
    Revision,
)
from opensquilla.artifact_session import (
    ArtifactNotFoundError as ArtifactSessionNotFoundError,
)
from opensquilla.artifacts import (
    ArtifactError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRef,
    ArtifactStore,
    artifact_payload,
)
from opensquilla.gateway.artifact_product_errors import (
    ArtifactProductErrorCode,
    artifact_product_error,
    logged_artifact_product_error,
)
from opensquilla.gateway.document_resource_recovery import DocumentImportRecoverySource
from opensquilla.gateway.event_bridge import EventBridge
from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    get_dispatcher,
)
from opensquilla.gateway.rpc_artifacts import _session_id_for_key
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.gateway.websocket import get_registry
from opensquilla.paths import media_root_from_config, native_io_path
from opensquilla.session.keys import canonicalize_session_key
from opensquilla.tools.builtin.document_format_adapters import (
    DocumentAdapterError,
    DocumentFormatAdapter,
    probe_document_format_adapter,
    validate_editable_html_source,
)

_d = get_dispatcher()

_ATTACHMENT_ID_RE = re.compile(r"^att_[A-Za-z0-9_-]{8,160}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_SOURCE_TYPES = frozenset({"attachment", "deliverable"})
_RESOURCE_TYPES = frozenset({"attachment", "document", "deliverable", "url"})
_RESOURCE_ID_FIELDS = {
    "attachment": "attachmentId",
    "document": "documentId",
    "deliverable": "artifactId",
    "url": "urlId",
}
_HTML_MIMES = frozenset({"text/html", "application/xhtml+xml"})
_OFFICE_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx"})
_MAX_RESOURCE_LIMIT = 500
_MAX_CURSOR_BYTES = 1024
_MAX_EDITABLE_HTML_BYTES = 2 * 1024 * 1024
_MAX_PREVIEWABLE_HTML_BYTES = 5 * 1024 * 1024
_CANDIDATE_PUBLICATION_WAIT_SECONDS = 5.0
_CANDIDATE_PUBLICATION_POLL_SECONDS = 0.01
_MUTATION_RESOLUTION_RETRY_AFTER_MS = 250
_MUTATION_OPERATION_TURN_PREFIX = {
    "source.patch": "manual-source-patch",
    "revision.restore": "revision-restore",
    "change.revert": "change-revert",
}
_MUTATION_IMPORT_OPERATIONS = frozenset(
    {"document.import", "workbench.resources.open"}
)
_MUTATION_PUBLISH_OPERATIONS = frozenset({"document.publish"})


@dataclass(frozen=True, slots=True)
class _AttachmentOccurrence:
    attachment_id: str
    message_id: str
    index: int
    name: str
    mime: str
    size: int
    sha256: str
    created_at: int
    payload: bytes
    durable: bool


@dataclass(frozen=True, slots=True)
class _ImportSource:
    source_type: DocumentSourceType
    resource_id: str
    name: str
    mime: str
    size: int
    sha256: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class _FormatProfile:
    kind: ArtifactKind
    adapter: DocumentFormatAdapter | None
    preview: bool
    editable: bool
    agent_editable: bool
    selection_context: bool
    publishable: bool
    reason_code: str | None


def _require_string(params: dict[str, Any] | None, name: str, *, max_bytes: int = 2048) -> str:
    if not isinstance(params, dict) or name not in params:
        raise ValueError(f"params.{name} is required")
    value = params[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"params.{name} must be a non-empty string")
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > max_bytes:
        raise ValueError(f"params.{name} is too long")
    return normalized


def _optional_string(
    params: dict[str, Any] | None,
    name: str,
    *,
    max_bytes: int = 2048,
) -> str | None:
    if not isinstance(params, dict) or params.get(name) is None:
        return None
    return _require_string(params, name, max_bytes=max_bytes)


def _idempotency_key(params: dict[str, Any] | None) -> str:
    """Accept the durable request identifier under either additive field name."""

    idempotency_key = _optional_string(params, "idempotencyKey", max_bytes=256)
    client_request_id = _optional_string(params, "clientRequestId", max_bytes=256)
    if (
        idempotency_key is not None
        and client_request_id is not None
        and idempotency_key != client_request_id
    ):
        raise ValueError("params.idempotencyKey and params.clientRequestId must match")
    request_id = idempotency_key or client_request_id
    if request_id is None:
        raise ValueError("params.idempotencyKey or params.clientRequestId is required")
    return request_id


def _mutation_resolution_request_id(params: dict[str, Any] | None) -> str:
    """Read the public request identity while retaining additive aliases."""

    request_id = _optional_string(params, "requestId", max_bytes=256)
    client_request_id = _optional_string(params, "clientRequestId", max_bytes=256)
    idempotency_key = _optional_string(params, "idempotencyKey", max_bytes=256)
    supplied = tuple(
        value for value in (request_id, client_request_id, idempotency_key) if value is not None
    )
    if not supplied or len(set(supplied)) != 1:
        raise artifact_product_error(ArtifactProductErrorCode.INVALID_REQUEST)
    return supplied[0]


def _required_sha256(params: dict[str, Any] | None, name: str) -> str:
    value = _require_string(params, name, max_bytes=64).lower()
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"params.{name} must be a SHA-256 digest")
    return value


def _bounded_limit(params: dict[str, Any] | None) -> int:
    value = params.get("limit", 100) if isinstance(params, dict) else 100
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("params.limit must be a positive integer")
    return min(value, _MAX_RESOURCE_LIMIT)


def _session_key(params: dict[str, Any] | None) -> str:
    return canonicalize_session_key(_require_string(params, "sessionKey"))


def _actor(ctx: RpcContext) -> Actor:
    public_id = getattr(ctx.principal, "token_public_id", None)
    actor_id = public_id if isinstance(public_id, str) and public_id else None
    if actor_id is None:
        actor_id = "local-owner" if ctx.principal.is_owner else ctx.principal.role
    return Actor(kind=ActorKind.USER, actor_id=actor_id)


async def _scope(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> tuple[str, str, ArtifactSessionService]:
    session_key = _session_key(params)
    session_id = await _session_id_for_key(ctx, session_key)
    if session_id is None:
        raise artifact_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            reason_code="session_unavailable",
        )
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise artifact_product_error(ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE)
    return (
        session_key,
        session_id,
        await ArtifactSessionService.from_session_storage(storage),
    )


def _not_found(resource_type: str, resource_id: str) -> RpcHandlerError:
    # Resource identifiers stay in diagnostics and inventory responses.  A
    # missing resource is one product recovery state regardless of its
    # internal storage kind.
    del resource_type, resource_id
    return artifact_product_error(
        ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
        reason_code="resource_unavailable",
    )


def _conflict(exc: Exception) -> RpcHandlerError:
    return logged_artifact_product_error(
        ArtifactProductErrorCode.DOCUMENT_CHANGED,
        exc,
        operation="workbench.resource_mutation",
    )


def _internal_product_error(
    code: ArtifactProductErrorCode,
    detail: str,
    *,
    operation: str,
    accepted: bool | None = False,
    retryable: bool = False,
) -> RpcHandlerError:
    """Keep an implementation diagnostic in logs while returning safe copy."""

    return logged_artifact_product_error(
        code,
        RuntimeError(detail),
        operation=operation,
        accepted=accepted,
        retryable=retryable,
    )


def _resource_ref_payload(resource_type: str, resource_id: str) -> dict[str, str]:
    """Serialize the public discriminated ref with its generic id alias."""

    id_field = _RESOURCE_ID_FIELDS.get(resource_type)
    if id_field is None:
        raise ValueError("resource type is unsupported")
    return {"type": resource_type, id_field: resource_id, "id": resource_id}


def _resource_ref(params: dict[str, Any] | None, name: str = "resource") -> tuple[str, str]:
    raw = params.get(name) if isinstance(params, dict) else None
    if not isinstance(raw, dict):
        raise ValueError(f"params.{name} must be an object")
    resource_type = _require_string(raw, "type", max_bytes=32).lower()
    if resource_type not in _RESOURCE_TYPES:
        raise ValueError(f"params.{name}.type is unsupported")
    id_field = _RESOURCE_ID_FIELDS[resource_type]
    canonical_id = _optional_string(raw, id_field, max_bytes=512)
    legacy_id = _optional_string(raw, "id", max_bytes=512)
    if canonical_id is not None and legacy_id is not None and canonical_id != legacy_id:
        raise ValueError(f"params.{name}.{id_field} and params.{name}.id must match")
    resource_id = canonical_id or legacy_id
    if resource_id is None:
        raise ValueError(f"params.{name}.{id_field} or params.{name}.id is required")
    return resource_type, resource_id


def _resource_ref_with_legacy_alias(
    params: dict[str, Any] | None,
) -> tuple[str, str]:
    """Read the canonical resourceRef field while accepting the resource alias."""

    if isinstance(params, dict) and isinstance(params.get("resourceRef"), dict):
        return _resource_ref(params, "resourceRef")
    return _resource_ref(params, "resource")


def _requested_types(params: dict[str, Any] | None) -> frozenset[str]:
    raw = params.get("types") if isinstance(params, dict) else None
    if raw is None:
        return _RESOURCE_TYPES
    if not isinstance(raw, list) or not raw:
        raise ValueError("params.types must be a non-empty array")
    values: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or item not in _RESOURCE_TYPES:
            raise ValueError("params.types contains an unsupported resource type")
        values.add(item)
    return frozenset(values)


def _resource_cursor_digest(resources: list[dict[str, Any]]) -> str:
    identity = "\0".join(
        ":".join(
            (
                str(item["resource"]["type"]),
                str(item["resource"]["id"]),
                str(item.get("sha256", "")),
                str(item.get("updatedAt", item.get("createdAt", ""))),
            )
        )
        for item in resources
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _encode_resource_cursor(
    *,
    session_id: str,
    requested_types: frozenset[str],
    resources: list[dict[str, Any]],
    offset: int,
) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "s": hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24],
            "t": sorted(requested_types),
            "d": _resource_cursor_digest(resources),
            "o": offset,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "wrc_" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_resource_cursor(
    params: dict[str, Any] | None,
    *,
    session_id: str,
    requested_types: frozenset[str],
    resources: list[dict[str, Any]],
) -> int:
    raw = params.get("cursor") if isinstance(params, dict) else None
    if raw is None:
        return 0
    if not isinstance(raw, str) or not raw.startswith("wrc_"):
        raise ValueError("params.cursor is invalid")
    if len(raw.encode("utf-8")) > _MAX_CURSOR_BYTES:
        raise ValueError("params.cursor is invalid")
    token = raw[4:]
    try:
        decoded = base64.b64decode(
            token + "=" * (-len(token) % 4),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ValueError("params.cursor is invalid") from None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ValueError("params.cursor is invalid")
    offset = payload.get("o")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 1:
        raise ValueError("params.cursor is invalid")
    expected = (
        hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24],
        sorted(requested_types),
        _resource_cursor_digest(resources),
    )
    actual = (payload.get("s"), payload.get("t"), payload.get("d"))
    if actual != expected or offset > len(resources):
        raise artifact_product_error(
            ArtifactProductErrorCode.DOCUMENT_CHANGED,
            reason_code="resource_list_changed",
        )
    return offset


def _safe_name(value: object) -> str:
    normalized = " ".join(str(value or "attachment").strip().split())
    return normalized[:160] or "attachment"


def _safe_mime(value: object) -> str:
    normalized = str(value or "application/octet-stream").split(";", 1)[0].strip().lower()
    if "/" not in normalized or any(char in normalized for char in "\r\n"):
        return "application/octet-stream"
    return normalized[:120]


def _legacy_attachment_id(
    *,
    session_id: str,
    message_id: str,
    index: int,
    sha256: str,
) -> str:
    digest = hashlib.sha256(
        f"{session_id}\0{message_id}\0{index}\0{sha256}".encode()
    ).digest()[:18]
    token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"att_legacy_{token}"


def _attachment_download_url(
    *,
    session_key: str,
    sha256: str,
    name: str,
    mime: str,
) -> str:
    return (
        f"/api/v1/attachments/{quote(sha256, safe='')}"
        f"?sessionKey={quote(session_key, safe='')}"
        f"&name={quote(name, safe='')}&mime={quote(mime, safe='')}"
    )


async def _attachment_occurrences(
    ctx: RpcContext,
    *,
    session_key: str,
    session_id: str,
) -> tuple[_AttachmentOccurrence, ...]:
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise _internal_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            "session storage is not wired",
            operation="workbench.attachments.list",
        )
    entries = await storage.get_canonical_transcript(session_id)
    occurrences: list[_AttachmentOccurrence] = []
    seen: dict[str, tuple[str, int, str]] = {}
    media_root = media_root_from_config(ctx.config)
    for entry in entries:
        if str(getattr(entry, "role", "")) != "user":
            continue
        raw_content = getattr(entry, "content", None)
        if not isinstance(raw_content, str):
            continue
        try:
            envelope = json.loads(raw_content)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(envelope, dict) or not isinstance(envelope.get("attachments"), list):
            continue
        message_id = str(getattr(entry, "message_id", "") or "")
        if not message_id:
            continue
        for index, item in enumerate(envelope["attachments"]):
            if not isinstance(item, dict) or item.get("missing_reason"):
                continue
            name = _safe_name(item.get("name"))
            mime = _safe_mime(item.get("mime") or item.get("type"))
            sha = str(item.get("sha256_ref") or "").lower()
            durable = bool(_SHA256_RE.fullmatch(sha))
            payload: bytes
            if durable:
                from opensquilla.attachment_refs import transcript_material_path

                try:
                    material_path = transcript_material_path(media_root, session_id, sha)
                    payload = native_io_path(material_path).read_bytes()
                except (OSError, ValueError):
                    continue
                if hashlib.sha256(payload).hexdigest() != sha:
                    continue
            else:
                data = item.get("data")
                if not isinstance(data, str):
                    continue
                try:
                    payload = base64.b64decode(data, validate=True)
                except (TypeError, ValueError):
                    continue
                sha = hashlib.sha256(payload).hexdigest()
            declared_size = item.get("size")
            if isinstance(declared_size, int) and not isinstance(declared_size, bool):
                if declared_size != len(payload):
                    continue
            attachment_id = str(item.get("attachment_id") or "")
            if not _ATTACHMENT_ID_RE.fullmatch(attachment_id):
                attachment_id = _legacy_attachment_id(
                    session_id=session_id,
                    message_id=message_id,
                    index=index,
                    sha256=sha,
                )
            identity = (message_id, index, sha)
            previous = seen.get(attachment_id)
            if previous is not None and previous != identity:
                raise _internal_product_error(
                    ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
                    "attachment resource identity collision",
                    operation="workbench.attachments.list",
                )
            seen[attachment_id] = identity
            occurrences.append(
                _AttachmentOccurrence(
                    attachment_id=attachment_id,
                    message_id=message_id,
                    index=index,
                    name=name,
                    mime=mime,
                    size=len(payload),
                    sha256=sha,
                    created_at=int(getattr(entry, "created_at", 0) or 0),
                    payload=payload,
                    durable=durable,
                )
            )
    return tuple(occurrences)


def _format_profile(
    name: str,
    mime: str,
    *,
    payload: bytes | None = None,
) -> _FormatProfile:
    adapter = probe_document_format_adapter(
        name=name,
        media_type=mime,
        source=payload,
    )
    if adapter is not None:
        capabilities = adapter.capabilities()
        if payload is not None:
            try:
                source = payload.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                return _FormatProfile(
                    kind=ArtifactKind.HTML,
                    adapter=adapter,
                    preview=False,
                    editable=False,
                    agent_editable=False,
                    selection_context=False,
                    publishable=True,
                    reason_code="html_encoding_unsupported",
                )
            try:
                adapter.validate(source)
            except DocumentAdapterError:
                return _FormatProfile(
                    kind=ArtifactKind.HTML,
                    adapter=adapter,
                    preview=False,
                    editable=False,
                    agent_editable=False,
                    selection_context=False,
                    publishable=True,
                    reason_code="html_validation_failed",
                )
        return _FormatProfile(
            kind=ArtifactKind.HTML,
            adapter=adapter,
            preview=capabilities.get("preview") is True,
            editable=capabilities.get("manualEdit") is True,
            agent_editable=capabilities.get("agentEdit") is True,
            selection_context=(
                capabilities.get("selectionContext") is True
                or capabilities.get("selection") is True
            ),
            publishable=True,
            reason_code=None,
        )
    suffix = Path(name).suffix.lower()
    if suffix == ".docx":
        kind = ArtifactKind.DOCUMENT
    elif suffix == ".xlsx":
        kind = ArtifactKind.SPREADSHEET
    elif suffix == ".pptx":
        kind = ArtifactKind.PRESENTATION
    else:
        kind = ArtifactKind.OTHER
    if suffix in _OFFICE_SUFFIXES:
        # Office material remains discoverable and downloadable, but neither
        # preview nor edit is advertised until a real renderer+adapter exists.
        return _FormatProfile(
            kind=kind,
            adapter=None,
            preview=False,
            editable=False,
            agent_editable=False,
            selection_context=False,
            publishable=False,
            reason_code="office_adapter_not_available",
        )
    normalized_mime = mime.split(";", 1)[0].strip().lower()
    preview = normalized_mime.startswith("image/") or normalized_mime == "application/pdf"
    return _FormatProfile(
        kind=kind,
        adapter=None,
        preview=preview,
        editable=False,
        agent_editable=False,
        selection_context=False,
        publishable=True,
        reason_code="format_edit_not_supported",
    )


def _kind_for(name: str, mime: str) -> ArtifactKind:
    return _format_profile(name, mime).kind


def _effective_edit_reason(profile: _FormatProfile, *, editable: bool) -> str | None:
    if editable and profile.editable:
        return None
    if profile.editable:
        return "html_bundle_edit_not_supported"
    return profile.reason_code


def _validated_import_source(source: _ImportSource) -> tuple[_ImportSource, DocumentFormatAdapter]:
    if not source.payload or len(source.payload) > _MAX_EDITABLE_HTML_BYTES:
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
            reason_code="html_edit_size_unsupported",
        )
    if b"\x00" in source.payload:
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
            reason_code="html_encoding_unsupported",
        )
    profile = _format_profile(source.name, source.mime, payload=source.payload)
    if profile.reason_code == "html_encoding_unsupported":
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
            reason_code="html_encoding_unsupported",
        )
    if profile.reason_code == "html_validation_failed":
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
            reason_code="html_validation_failed",
        )
    if profile.adapter is None or profile.adapter.format_id != "html" or not profile.editable:
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
            reason_code=profile.reason_code or "format_edit_not_supported",
        )
    try:
        text = source.payload.decode("utf-8")
        validate_editable_html_source(text)
    except UnicodeDecodeError:
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
            reason_code="html_encoding_unsupported",
        ) from None
    except DocumentAdapterError:
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
            reason_code="html_validation_failed",
        ) from None
    if source.mime.split(";", 1)[0].strip().lower() not in _HTML_MIMES:
        source = _ImportSource(
            source_type=source.source_type,
            resource_id=source.resource_id,
            name=source.name,
            mime="text/html",
            size=source.size,
            sha256=source.sha256,
            payload=source.payload,
        )
    return source, profile.adapter


async def adopt_generated_deliverable_if_editable(
    *,
    service: ArtifactSessionService,
    store: ArtifactStore,
    session_key: str,
    session_id: str,
    ref: ArtifactRef,
    actor: Actor | None = None,
) -> tuple[Document, Revision, DocumentSourceBinding, bool] | None:
    """Materialize one generated single-file HTML deliverable as a Document.

    Unsupported material returns ``None`` so publication remains successful
    and read-only. Storage or integrity failures still raise so the turn
    boundary can record a recoverable materialization failure.
    """

    editable = await asyncio.to_thread(
        store.supports_single_file_editing,
        ref.id,
        session_id=session_id,
    )
    if not editable:
        return None
    resolved, path = await asyncio.to_thread(
        store.resolve_for_download,
        ref.id,
        session_id=session_id,
    )
    payload = await asyncio.to_thread(native_io_path(path).read_bytes)
    if (
        resolved.sha256 != ref.sha256
        or resolved.name != ref.name
        or resolved.mime != ref.mime
        or resolved.size != ref.size
    ):
        raise ArtifactIntegrityError("generated deliverable identity changed")
    source = _ImportSource(
        source_type=DocumentSourceType.DELIVERABLE,
        resource_id=resolved.id,
        name=resolved.name,
        mime=resolved.mime,
        size=resolved.size,
        sha256=resolved.sha256,
        payload=payload,
    )
    try:
        _validated_import_source(source)
    except RpcHandlerError as exc:
        if exc.code == ArtifactProductErrorCode.RESOURCE_UNSUPPORTED.value:
            return None
        raise
    commit, binding, created = await service.adopt_generated_deliverable(
        session_key=session_key,
        session_id=session_id,
        name=resolved.name,
        kind=_kind_for(resolved.name, resolved.mime),
        deliverable=ArtifactBlobRef(
            artifact_id=resolved.id,
            sha256=resolved.sha256,
            filename=resolved.name,
            media_type=resolved.mime,
            byte_size=resolved.size,
        ),
        actor=actor or Actor(kind=ActorKind.SYSTEM, actor_id="generated-deliverable"),
    )
    return commit.document, commit.revision, binding, created


def _attachment_payload(
    occurrence: _AttachmentOccurrence,
    *,
    session_key: str,
    binding: DocumentSourceBinding | None,
    include_inline_url: bool,
) -> dict[str, Any]:
    base_profile = _format_profile(occurrence.name, occurrence.mime)
    html_resource = base_profile.adapter is not None and base_profile.adapter.format_id == "html"
    preview_reason_code: str | None
    edit_reason_code: str | None
    if html_resource and occurrence.size > _MAX_PREVIEWABLE_HTML_BYTES:
        # Do not parse material that the client is required to reject before
        # rendering. Advertise the same limit at discovery time so the chat
        # action cannot become a silent, doomed click.
        preview = False
        editable = False
        preview_reason_code = "html_preview_size_unsupported"
        edit_reason_code = "html_edit_size_unsupported"
    else:
        profile = _format_profile(
            occurrence.name,
            occurrence.mime,
            payload=occurrence.payload,
        )
        preview = profile.preview
        editable = profile.editable
        preview_reason_code = profile.reason_code if not preview else None
        edit_reason_code = profile.reason_code if not editable else None
        if html_resource and occurrence.size > _MAX_EDITABLE_HTML_BYTES:
            editable = False
            edit_reason_code = "html_edit_size_unsupported"
    payload: dict[str, Any] = {
        "resource": _resource_ref_payload("attachment", occurrence.attachment_id),
        "name": occurrence.name,
        "mime": occurrence.mime,
        "size": occurrence.size,
        "sha256": occurrence.sha256,
        "createdAt": occurrence.created_at,
        "capabilities": {
            "preview": preview,
            "download": True,
            # Immutable sources must be copied into a Document before any
            # selection-scoped or Agent mutation capability can exist.
            "selectionContext": False,
            "manualEdit": editable,
            "agentEdit": False,
            # Compatibility summary retained for clients predating the
            # independent Workbench capability axes.
            "edit": editable,
            "publish": False,
            "previewReasonCode": preview_reason_code,
            "editReasonCode": edit_reason_code,
        },
        "relations": {
            "messageId": occurrence.message_id,
            "attachmentIndex": occurrence.index,
            "sourceSha256": occurrence.sha256,
        },
    }
    if binding is not None:
        payload["relations"]["documentId"] = binding.document_id
    if occurrence.durable:
        payload["downloadUrl"] = _attachment_download_url(
            session_key=session_key,
            sha256=occurrence.sha256,
            name=occurrence.name,
            mime=occurrence.mime,
        )
    elif include_inline_url:
        encoded = base64.b64encode(occurrence.payload).decode("ascii")
        payload["downloadUrl"] = f"data:{occurrence.mime};base64,{encoded}"
    return payload


def _document_payload(
    document: Document,
    head: Revision,
    *,
    binding: DocumentSourceBinding | None,
    publication: DocumentPublication | None,
    trusted_capabilities: bool,
) -> dict[str, Any]:
    profile = _format_profile(document.name, head.media_type)
    effective_editable = trusted_capabilities and profile.editable
    effective_agent_editable = trusted_capabilities and profile.agent_editable
    effective_selection_context = trusted_capabilities and profile.selection_context
    legacy_edit = effective_editable or effective_agent_editable
    relations: dict[str, Any] = {
        "documentId": document.document_id,
        "headRevisionId": head.revision_id,
        "headArtifactId": head.artifact_id,
    }
    if binding is not None:
        relations["source"] = _resource_ref_payload(
            binding.source_type.value,
            binding.source_resource_id,
        )
        relations["sourceSha256"] = binding.source_sha256
    if publication is not None:
        relations["deliverableId"] = publication.deliverable_artifact_id
        relations["publishedRevisionId"] = publication.revision_id
    return {
        "resource": _resource_ref_payload("document", document.document_id),
        "name": document.name,
        "mime": head.media_type,
        "size": head.byte_size,
        "sha256": head.artifact_sha256,
        "createdAt": document.created_at,
        "updatedAt": document.updated_at,
        "downloadUrl": (
            f"/api/v1/artifact-documents/{quote(document.document_id, safe='')}"
            f"?revisionId={quote(head.revision_id, safe='')}"
        ),
        "capabilities": {
            "preview": profile.preview,
            "download": True,
            "selectionContext": effective_selection_context,
            "manualEdit": effective_editable,
            "agentEdit": effective_agent_editable,
            # Compatibility summary retained for clients predating the
            # independent Workbench capability axes.
            "edit": legacy_edit,
            "publish": profile.publishable,
            "editReasonCode": _effective_edit_reason(
                profile,
                editable=effective_editable,
            ),
        },
        "relations": relations,
    }


def _deliverable_payload(
    ref: ArtifactRef,
    *,
    publication: DocumentPublication | None,
    binding: DocumentSourceBinding | None,
    importable: bool,
) -> dict[str, Any]:
    profile = _format_profile(ref.name, ref.mime)
    effective_importable = importable and profile.editable
    relations: dict[str, Any] = {}
    if publication is not None:
        relations.update(
            {
                "documentId": publication.document_id,
                "publishedRevisionId": publication.revision_id,
                "publicationId": publication.publication_id,
            }
        )
    elif binding is not None:
        relations["documentId"] = binding.document_id
    return {
        "resource": _resource_ref_payload("deliverable", ref.id),
        "name": ref.name,
        "mime": ref.mime,
        "size": ref.size,
        "sha256": ref.sha256,
        "createdAt": ref.created_at,
        "downloadUrl": ref.download_url,
        "capabilities": {
            "preview": profile.preview,
            "download": True,
            # Published artifacts remain immutable until copied into a
            # Document; preview never grants selection or mutation authority.
            "selectionContext": False,
            "manualEdit": effective_importable,
            "agentEdit": False,
            # Compatibility summary retained for clients predating the
            # independent Workbench capability axes.
            "edit": effective_importable,
            "publish": False,
            "editReasonCode": _effective_edit_reason(
                profile,
                editable=effective_importable,
            ),
        },
        "relations": relations,
    }


async def _public_deliverables(
    ctx: RpcContext,
    *,
    session_id: str,
) -> tuple[ArtifactRef, ...]:
    store = ArtifactStore(media_root_from_config(ctx.config))
    try:
        page = await asyncio.to_thread(
            store.list_refs,
            session_id=session_id,
            limit=100_000,
        )
    except OSError as exc:
        raise logged_artifact_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            exc,
            operation="workbench.deliverables.list",
            retryable=True,
        ) from exc
    return page.refs


async def _scoped_document(
    service: ArtifactSessionService,
    *,
    session_key: str,
    session_id: str,
    document_id: str,
) -> tuple[Document, Revision]:
    try:
        current = await service.get_document_head(document_id)
    except ArtifactSessionNotFoundError:
        raise _not_found("document", document_id) from None
    document = current.document
    if document.session_key != session_key or document.session_id != session_id:
        raise _not_found("document", document_id)
    return document, current.revision


async def _resource_inventory(
    ctx: RpcContext,
    *,
    session_key: str,
    session_id: str,
    service: ArtifactSessionService,
    include_inline_urls: bool,
) -> dict[str, list[dict[str, Any]]]:
    documents, bindings, publications, attachments, deliverables = await asyncio.gather(
        service.list_documents(session_key=session_key, session_id=session_id, limit=1000),
        service.list_document_source_bindings(session_id=session_id, limit=1000),
        service.list_document_publications(session_id=session_id, limit=1000),
        _attachment_occurrences(ctx, session_key=session_key, session_id=session_id),
        _public_deliverables(ctx, session_id=session_id),
    )
    binding_by_document = {item.document_id: item for item in bindings}
    binding_by_source = {
        (item.source_type.value, item.source_resource_id): item for item in bindings
    }
    publication_by_artifact = {
        item.deliverable_artifact_id: item for item in publications
    }
    store = ArtifactStore(media_root_from_config(ctx.config))
    deliverable_importable: dict[str, bool] = {}
    for deliverable in deliverables:
        if not _format_profile(deliverable.name, deliverable.mime).editable:
            deliverable_importable[deliverable.id] = False
            continue
        try:
            importable = await asyncio.to_thread(
                store.supports_single_file_editing,
                deliverable.id,
                session_id=session_id,
            )
        except (ArtifactError, OSError):
            deliverable_importable[deliverable.id] = False
        else:
            deliverable_importable[deliverable.id] = importable
    latest_publication_by_document: dict[str, DocumentPublication] = {}
    for publication in publications:
        latest_publication_by_document.setdefault(publication.document_id, publication)

    document_resources: list[dict[str, Any]] = []
    for document in documents:
        head = await service.get_revision(document.head_revision_id)
        if head.document_id != document.document_id:
            raise _internal_product_error(
                ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
                "document head integrity check failed",
                operation="workbench.resources.list",
            )
        profile = _format_profile(document.name, head.media_type)
        trusted_capabilities = (
            profile.editable or profile.agent_editable or profile.selection_context
        )
        if trusted_capabilities:
            try:
                trusted_capabilities = await asyncio.to_thread(
                    store.supports_single_file_editing,
                    head.artifact_id,
                    session_id=session_id,
                )
            except (ArtifactError, OSError):
                trusted_capabilities = False
        document_resources.append(
            _document_payload(
                document,
                head,
                binding=binding_by_document.get(document.document_id),
                publication=latest_publication_by_document.get(document.document_id),
                trusted_capabilities=trusted_capabilities,
            )
        )
    attachment_resources = [
        _attachment_payload(
            item,
            session_key=session_key,
            binding=binding_by_source.get(("attachment", item.attachment_id)),
            include_inline_url=include_inline_urls,
        )
        for item in attachments
    ]
    deliverable_resources = [
        _deliverable_payload(
            item,
            publication=publication_by_artifact.get(item.id),
            binding=binding_by_source.get(("deliverable", item.id)),
            importable=deliverable_importable.get(item.id, False),
        )
        for item in deliverables
    ]
    return {
        "attachment": attachment_resources,
        "document": document_resources,
        "deliverable": deliverable_resources,
        # Reserved protocol type: inventory does not fetch or persist network content.
        "url": [],
    }


async def _preview_material(
    ctx: RpcContext,
    *,
    session_key: str,
    session_id: str,
    service: ArtifactSessionService,
    resource_type: str,
    resource_id: str,
) -> tuple[dict[str, Any], bytes]:
    inventory = await _resource_inventory(
        ctx,
        session_key=session_key,
        session_id=session_id,
        service=service,
        include_inline_urls=True,
    )
    resource = next(
        (
            item
            for item in inventory[resource_type]
            if item["resource"]["id"] == resource_id
        ),
        None,
    )
    if resource is None:
        raise _not_found(resource_type, resource_id)
    capabilities = resource.get("capabilities")
    if not isinstance(capabilities, dict) or capabilities.get("preview") is not True:
        reason = (
            str(
                capabilities.get("previewReasonCode")
                or capabilities.get("editReasonCode")
                or "preview_not_supported"
            )
            if isinstance(capabilities, dict)
            else "preview_not_supported"
        )
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
            reason_code=reason,
        )

    if resource_type == "attachment":
        occurrence = next(
            (
                item
                for item in await _attachment_occurrences(
                    ctx,
                    session_key=session_key,
                    session_id=session_id,
                )
                if item.attachment_id == resource_id
            ),
            None,
        )
        if occurrence is None:
            raise _not_found(resource_type, resource_id)
        payload = occurrence.payload
    else:
        if resource_type == "document":
            _document, revision = await _scoped_document(
                service,
                session_key=session_key,
                session_id=session_id,
                document_id=resource_id,
            )
            artifact_id = revision.artifact_id
        elif resource_type == "deliverable":
            ref = next(
                (
                    item
                    for item in await _public_deliverables(ctx, session_id=session_id)
                    if item.id == resource_id
                ),
                None,
            )
            if ref is None:
                raise _not_found(resource_type, resource_id)
            artifact_id = ref.id
        else:
            raise _not_found(resource_type, resource_id)
        store = ArtifactStore(media_root_from_config(ctx.config))
        try:
            _ref, path = await asyncio.to_thread(
                store.resolve_for_download,
                artifact_id,
                session_id=session_id,
            )
            payload = await asyncio.to_thread(native_io_path(path).read_bytes)
        except (ArtifactNotFoundError, ArtifactIntegrityError, OSError, ValueError):
            raise _not_found(resource_type, resource_id) from None

    expected_sha = str(resource.get("sha256") or "")
    expected_size = resource.get("size")
    if (
        (_SHA256_RE.fullmatch(expected_sha) and hashlib.sha256(payload).hexdigest() != expected_sha)
        or (
            isinstance(expected_size, int)
            and not isinstance(expected_size, bool)
            and len(payload) != expected_size
        )
    ):
        raise _internal_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            "workbench preview material failed integrity validation",
            operation="workbench.resources.preview",
        )
    return resource, payload


def _preview_payload(resource: dict[str, Any], payload: bytes, *, mode: str) -> dict[str, Any]:
    profile = _format_profile(
        str(resource.get("name") or ""),
        str(resource.get("mime") or ""),
        payload=payload,
    )
    adapter_payload: dict[str, object] | None = None
    if profile.adapter is not None:
        try:
            source = payload.decode("utf-8")
            adapter_payload = profile.adapter.preview(source)
        except UnicodeDecodeError:
            raise artifact_product_error(
                ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
                reason_code="html_encoding_unsupported",
            ) from None
        except DocumentAdapterError:
            raise artifact_product_error(
                ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
                reason_code="html_validation_failed",
            ) from None
    return {
        "protocolVersion": 1,
        "mode": mode,
        "resource": resource["resource"],
        "launchUrl": resource.get("downloadUrl"),
        "sandboxProfile": "opaque-offline",
        "network": False,
        "adapter": adapter_payload,
    }


async def _resolve_import_source(
    ctx: RpcContext,
    *,
    session_key: str,
    session_id: str,
    source_type: str,
    resource_id: str,
) -> _ImportSource:
    if source_type == "attachment":
        occurrences = await _attachment_occurrences(
            ctx,
            session_key=session_key,
            session_id=session_id,
        )
        occurrence = next(
            (item for item in occurrences if item.attachment_id == resource_id),
            None,
        )
        if occurrence is None:
            raise _not_found(source_type, resource_id)
        return _ImportSource(
            source_type=DocumentSourceType.ATTACHMENT,
            resource_id=resource_id,
            name=occurrence.name,
            mime=occurrence.mime,
            size=occurrence.size,
            sha256=occurrence.sha256,
            payload=occurrence.payload,
        )

    refs = await _public_deliverables(ctx, session_id=session_id)
    ref = next((item for item in refs if item.id == resource_id), None)
    if ref is None:
        raise _not_found(source_type, resource_id)
    store = ArtifactStore(media_root_from_config(ctx.config))
    try:
        importable = await asyncio.to_thread(
            store.supports_single_file_editing,
            ref.id,
            session_id=session_id,
        )
        if not importable:
            raise artifact_product_error(
                ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
                reason_code="html_bundle_edit_not_supported",
            )
        resolved, path = await asyncio.to_thread(
            store.resolve_for_download,
            ref.id,
            session_id=session_id,
        )
        payload = await asyncio.to_thread(native_io_path(path).read_bytes)
    except (ArtifactNotFoundError, ArtifactIntegrityError, OSError, ValueError):
        raise _not_found(source_type, resource_id) from None
    return _ImportSource(
        source_type=DocumentSourceType.DELIVERABLE,
        resource_id=resource_id,
        name=resolved.name,
        mime=resolved.mime,
        size=resolved.size,
        sha256=resolved.sha256,
        payload=payload,
    )


async def resolve_recovery_import_source(
    ctx: RpcContext,
    attempt: DocumentImportAttempt,
) -> DocumentImportRecoverySource | None:
    """Re-resolve and validate immutable import material during startup recovery."""

    scoped_session_id = await _session_id_for_key(ctx, attempt.session_key)
    if scoped_session_id != attempt.session_id:
        return None
    try:
        source = await _resolve_import_source(
            ctx,
            session_key=attempt.session_key,
            session_id=attempt.session_id,
            source_type=attempt.source_type.value,
            resource_id=attempt.source_resource_id,
        )
        source, _adapter = _validated_import_source(source)
    except RpcHandlerError:
        return None
    return DocumentImportRecoverySource(
        session_key=attempt.session_key,
        session_id=attempt.session_id,
        source_type=source.source_type,
        resource_id=source.resource_id,
        name=source.name,
        mime=source.mime,
        size=source.size,
        sha256=source.sha256,
        payload=source.payload,
    )


def _candidate_blob(attempt: DocumentImportAttempt) -> ArtifactBlobRef:
    return ArtifactBlobRef(
        artifact_id=attempt.candidate_artifact_id,
        sha256=attempt.source_sha256,
        filename=attempt.document_name,
        media_type=attempt.source_mime,
        byte_size=attempt.source_size,
    )


async def _ensure_internal_candidate(
    ctx: RpcContext,
    *,
    session_key: str,
    session_id: str,
    artifact: ArtifactBlobRef,
    payload: bytes | None,
    source: str,
) -> ArtifactRef:
    store = ArtifactStore(media_root_from_config(ctx.config))
    deadline = asyncio.get_running_loop().time() + _CANDIDATE_PUBLICATION_WAIT_SECONDS
    while True:
        try:
            existing, _path = await asyncio.to_thread(
                store.resolve_for_download,
                artifact.artifact_id,
                session_id=session_id,
            )
        except ArtifactNotFoundError:
            if payload is None:
                raise _internal_product_error(
                    ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
                    "journaled document bytes are unavailable",
                    operation="documents.candidate.restore",
                ) from None
            try:
                existing = await asyncio.to_thread(
                    store.publish_bytes,
                    payload,
                    session_id=session_id,
                    session_key=session_key,
                    name=artifact.filename,
                    mime=artifact.media_type,
                    source=source,
                    visibility="internal",
                    artifact_id=artifact.artifact_id,
                )
            except FileExistsError:
                # Another request owns this candidate bucket but may not have
                # made meta.json visible yet. Never delete or rewrite that
                # in-flight bucket: wait asynchronously, then resolve it. If
                # its publisher rolls the partial bucket back, the next loop
                # safely retries publication from the journaled source bytes.
                if asyncio.get_running_loop().time() >= deadline:
                    raise _internal_product_error(
                        ArtifactProductErrorCode.MUTATION_OUTCOME_PENDING,
                        "journaled document candidate publication is still pending",
                        operation="documents.candidate.publish",
                        accepted=None,
                    ) from None
                await asyncio.sleep(_CANDIDATE_PUBLICATION_POLL_SECONDS)
                continue
            except (ArtifactError, OSError) as exc:
                raise logged_artifact_product_error(
                    ArtifactProductErrorCode.MUTATION_OUTCOME_PENDING,
                    exc,
                    operation="documents.candidate.publish",
                    accepted=None,
                ) from exc
        except (ArtifactIntegrityError, OSError, ValueError) as exc:
            raise logged_artifact_product_error(
                ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
                exc,
                operation="documents.candidate.read",
            ) from exc
        break
    if (
        existing.sha256 != artifact.sha256
        or existing.name != artifact.filename
        or existing.mime != artifact.media_type
        or existing.size != artifact.byte_size
    ):
        raise _internal_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            "journaled document candidate integrity mismatch",
            operation="documents.candidate.verify",
        )
    return existing


async def _discard_unused_candidate(
    ctx: RpcContext,
    *,
    service: ArtifactSessionService,
    session_id: str,
    idempotency_key: str,
    candidate_artifact_id: str,
    result_artifact_id: str,
) -> None:
    if candidate_artifact_id == result_artifact_id:
        return
    store = ArtifactStore(media_root_from_config(ctx.config))
    try:
        deleted = await asyncio.to_thread(
            store.delete_ref,
            session_id=session_id,
            artifact_id=candidate_artifact_id,
        )
    except (ArtifactError, OSError) as exc:
        raise logged_artifact_product_error(
            ArtifactProductErrorCode.MUTATION_OUTCOME_PENDING,
            exc,
            operation="documents.import.cleanup",
            accepted=None,
        ) from exc
    if not deleted:
        try:
            await asyncio.to_thread(
                store.resolve_for_download,
                candidate_artifact_id,
                session_id=session_id,
            )
        except ArtifactNotFoundError:
            pass
        except (ArtifactError, OSError) as exc:
            raise logged_artifact_product_error(
                ArtifactProductErrorCode.MUTATION_OUTCOME_PENDING,
                exc,
                operation="documents.import.cleanup",
                accepted=None,
            ) from exc
        else:
            raise _internal_product_error(
                ArtifactProductErrorCode.MUTATION_OUTCOME_PENDING,
                "document import committed but candidate cleanup is pending",
                operation="documents.import.cleanup",
                accepted=None,
            )
    try:
        await service.mark_document_import_candidate_cleaned(
            session_id=session_id,
            idempotency_key=idempotency_key,
        )
    except (ArtifactConflictError, ArtifactValidationError) as exc:
        raise logged_artifact_product_error(
            ArtifactProductErrorCode.MUTATION_OUTCOME_PENDING,
            exc,
            operation="documents.import.cleanup",
            accepted=None,
        ) from exc


def _binding_payload(binding: DocumentSourceBinding) -> dict[str, Any]:
    return {
        "id": binding.binding_id,
        "bindingId": binding.binding_id,
        "documentId": binding.document_id,
        "source": _resource_ref_payload(
            binding.source_type.value,
            binding.source_resource_id,
        ),
        "sourceSha256": binding.source_sha256,
        "sourceName": binding.source_name,
        "sourceMime": binding.source_mime,
        "sourceSize": binding.source_size,
        "mode": binding.mode.value,
        "createdAt": binding.created_at,
    }


def _revision_payload(revision: Revision) -> dict[str, Any]:
    return {
        "id": revision.revision_id,
        "revisionId": revision.revision_id,
        "documentId": revision.document_id,
        "artifactId": revision.artifact_id,
        "sha256": revision.artifact_sha256,
        "name": revision.filename,
        "mime": revision.media_type,
        "size": revision.byte_size,
        "generation": revision.generation,
        "createdAt": revision.created_at,
        "downloadUrl": (
            f"/api/v1/artifact-documents/{quote(revision.document_id, safe='')}"
            f"?revisionId={quote(revision.revision_id, safe='')}"
        ),
    }


def _document_rpc_payload_from_parts(
    document: Document,
    revision: Revision,
) -> dict[str, Any]:
    return {
        "id": document.document_id,
        "documentId": document.document_id,
        "sessionKey": document.session_key,
        "sessionId": document.session_id,
        "name": document.name,
        "kind": document.kind.value,
        "headRevisionId": document.head_revision_id,
        "generation": document.generation,
        "stateRevision": document.state_revision,
        "latestDownloadUrl": f"/api/v1/artifact-documents/{document.document_id}",
        "createdAt": document.created_at,
        "updatedAt": document.updated_at,
        "head": _revision_payload(revision),
    }


def _document_rpc_payload(result: DocumentImportResult) -> dict[str, Any]:
    return _document_rpc_payload_from_parts(
        result.commit.document,
        result.commit.revision,
    )


def _import_response(result: DocumentImportResult, *, replayed: bool) -> dict[str, Any]:
    return {
        "document": _document_rpc_payload(result),
        "revision": _revision_payload(result.commit.revision),
        "binding": _binding_payload(result.binding),
        "receipt": {
            "attemptId": result.attempt.attempt_id,
            "requestId": result.attempt.idempotency_key,
            "idempotencyKey": result.attempt.idempotency_key,
            "status": result.attempt.status.value,
            "replayed": replayed,
        },
    }


async def import_document_from_resource(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Shared implementation used by documents.import and the legacy open adapter."""

    session_key, session_id, service = await _scope(params, ctx)
    source_type, resource_id = _resource_ref(params, "source")
    if source_type not in _SUPPORTED_SOURCE_TYPES:
        raise ValueError("params.source.type must be attachment or deliverable")
    mode = _require_string(params, "mode", max_bytes=16).lower()
    if mode != DocumentImportMode.COPY.value:
        raise ValueError("params.mode must be copy")
    idempotency_key = _idempotency_key(params)
    requested_name = _optional_string(params, "name", max_bytes=512)
    expected_sha256 = _required_sha256(params, "expectedSha256")

    replayed = False
    try:
        attempt = await service.get_document_import_attempt(
            session_id=session_id,
            idempotency_key=idempotency_key,
        )
    except ArtifactSessionNotFoundError:
        attempt = None
    if attempt is not None:
        effective_requested_name = requested_name or attempt.source_name
        if (
            attempt.session_key != session_key
            or attempt.source_type.value != source_type
            or attempt.source_resource_id != resource_id
            or attempt.mode is not DocumentImportMode.COPY
            or effective_requested_name != attempt.document_name
            or expected_sha256 != attempt.source_sha256
        ):
            raise _conflict(
                ArtifactConflictError(
                    "document import idempotency key was reused with different input"
                )
            )
        if attempt.status in {MutationAttemptStatus.FAILED, MutationAttemptStatus.AMBIGUOUS}:
            raise _conflict(
                ArtifactConflictError(
                    f"document import attempt is terminal: {attempt.status.value}"
                )
            )
        replayed = True

    source: _ImportSource | None = None
    if attempt is None:
        source = await _resolve_import_source(
            ctx,
            session_key=session_key,
            session_id=session_id,
            source_type=source_type,
            resource_id=resource_id,
        )
        source, _adapter = _validated_import_source(source)
        if expected_sha256 != source.sha256:
            raise _conflict(ArtifactConflictError("document import source hash changed"))
        document_name = requested_name or source.name
        candidate_id = ArtifactStore.allocate_artifact_id()
        try:
            attempt, created = await service.reserve_document_import_attempt(
                session_key=session_key,
                session_id=session_id,
                idempotency_key=idempotency_key,
                source_type=source.source_type,
                source_resource_id=source.resource_id,
                source_sha256=source.sha256,
                source_name=source.name,
                source_mime=source.mime,
                source_size=source.size,
                document_name=document_name,
                mode=DocumentImportMode.COPY,
                candidate_artifact_id=candidate_id,
            )
        except (ArtifactConflictError, ArtifactValidationError) as exc:
            raise _conflict(exc) from exc
        replayed = not created

    assert attempt is not None
    candidate = _candidate_blob(attempt)
    if attempt.status is MutationAttemptStatus.APPLIED:
        result = await service.apply_document_import_attempt(
            session_id=session_id,
            idempotency_key=idempotency_key,
            candidate_artifact=candidate,
            document_name=attempt.document_name,
            kind=_kind_for(attempt.document_name, attempt.source_mime),
            actor=_actor(ctx),
        )
        await _discard_unused_candidate(
            ctx,
            service=service,
            session_id=session_id,
            idempotency_key=idempotency_key,
            candidate_artifact_id=attempt.candidate_artifact_id,
            result_artifact_id=result.commit.revision.artifact_id,
        )
        return _import_response(result, replayed=True)

    payload: bytes | None = source.payload if source is not None else None
    if payload is None:
        try:
            await _ensure_internal_candidate(
                ctx,
                session_key=session_key,
                session_id=session_id,
                artifact=candidate,
                payload=None,
                source="document_import",
            )
        except RpcHandlerError as exc:
            if exc.code not in {
                ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE.value,
                ArtifactProductErrorCode.MUTATION_OUTCOME_PENDING.value,
            }:
                raise
            source = await _resolve_import_source(
                ctx,
                session_key=session_key,
                session_id=session_id,
                source_type=source_type,
                resource_id=resource_id,
            )
            source, _adapter = _validated_import_source(source)
            if (
                source.sha256 != attempt.source_sha256
                or source.name != attempt.source_name
                or source.mime != attempt.source_mime
                or source.size != attempt.source_size
            ):
                raise _conflict(ArtifactConflictError("document import source changed"))
            payload = source.payload
    try:
        await _ensure_internal_candidate(
            ctx,
            session_key=session_key,
            session_id=session_id,
            artifact=candidate,
            payload=payload,
            source="document_import",
        )
        result = await service.apply_document_import_attempt(
            session_id=session_id,
            idempotency_key=idempotency_key,
            candidate_artifact=candidate,
            document_name=attempt.document_name,
            kind=_kind_for(attempt.document_name, attempt.source_mime),
            actor=_actor(ctx),
        )
    except (ArtifactConflictError, ArtifactValidationError) as exc:
        try:
            await service.fail_document_import_attempt(
                session_id=session_id,
                idempotency_key=idempotency_key,
                failure_code="IMPORT_COMMIT_FAILED",
            )
        except Exception:
            pass
        raise _conflict(exc) from exc
    await _discard_unused_candidate(
        ctx,
        service=service,
        session_id=session_id,
        idempotency_key=idempotency_key,
        candidate_artifact_id=attempt.candidate_artifact_id,
        result_artifact_id=result.commit.revision.artifact_id,
    )
    return _import_response(result, replayed=replayed)


def _publication_payload(publication: DocumentPublication) -> dict[str, Any]:
    return {
        "id": publication.publication_id,
        "publicationId": publication.publication_id,
        "documentId": publication.document_id,
        "revisionId": publication.revision_id,
        "deliverableId": publication.deliverable_artifact_id,
        "artifactId": publication.deliverable_artifact_id,
        "sha256": publication.artifact_sha256,
        "name": publication.name,
        "mime": publication.mime,
        "size": publication.size,
        "createdAt": publication.created_at,
    }


def _publish_response(
    result: DocumentPublishResult,
    ref: ArtifactRef,
    *,
    replayed: bool,
) -> dict[str, Any]:
    return {
        "deliverable": artifact_payload(ref),
        "publication": _publication_payload(result.publication),
        "receipt": {
            "attemptId": result.attempt.attempt_id,
            "requestId": result.attempt.idempotency_key,
            "idempotencyKey": result.attempt.idempotency_key,
            "status": result.attempt.status.value,
            "replayed": replayed,
        },
    }


async def _emit_publish_events(
    ctx: RpcContext,
    *,
    session_key: str,
    service: ArtifactSessionService,
    result: DocumentPublishResult,
    ref: ArtifactRef,
) -> None:
    bridge = EventBridge(ctx.subscription_manager, get_registry())
    await bridge.emit(session_key, "session.event.artifact", artifact_payload(ref))
    latest = await service.latest_audit_event(result.publication.document_id)
    if latest is None:
        return
    payload = {
        "artifactEventSeq": latest.sequence,
        "documentId": result.publication.document_id,
        "revisionId": result.publication.revision_id,
        "changeSetId": None,
        "action": "document.published",
    }
    await bridge.emit(session_key, "session.event.artifact_state", payload)
    await bridge.emit(session_key, "document.state_changed", payload)


@_d.method("workbench.resources.list", scope="operator.read")
async def _handle_resources_list(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    requested = _requested_types(params)
    limit = _bounded_limit(params)
    inventory = await _resource_inventory(
        ctx,
        session_key=session_key,
        session_id=session_id,
        service=service,
        include_inline_urls=False,
    )
    selected = [
        item
        for kind in ("document", "attachment", "deliverable", "url")
        if kind in requested
        for item in inventory[kind]
    ]
    offset = _decode_resource_cursor(
        params,
        session_id=session_id,
        requested_types=requested,
        resources=selected,
    )
    page = selected[offset : offset + limit]
    next_offset = offset + len(page)
    next_cursor = (
        _encode_resource_cursor(
            session_id=session_id,
            requested_types=requested,
            resources=selected,
            offset=next_offset,
        )
        if next_offset < len(selected)
        else None
    )
    return {
        "resources": page,
        "totalCount": len(selected),
        "pageSize": limit,
        "returnedCount": len(page),
        "hasMore": next_cursor is not None,
        "nextCursor": next_cursor,
    }


@_d.method("workbench.resources.get", scope="operator.read")
async def _handle_resources_get(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    resource_type, resource_id = _resource_ref_with_legacy_alias(params)
    inventory = await _resource_inventory(
        ctx,
        session_key=session_key,
        session_id=session_id,
        service=service,
        include_inline_urls=True,
    )
    resource = next(
        (
            item
            for item in inventory[resource_type]
            if item["resource"]["id"] == resource_id
        ),
        None,
    )
    if resource is None:
        raise _not_found(resource_type, resource_id)
    return {"resource": resource}


async def _current_document_open_response(
    ctx: RpcContext,
    *,
    session_key: str,
    session_id: str,
    service: ArtifactSessionService,
    document_id: str,
    materialized: bool,
    receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document, head = await _scoped_document(
        service,
        session_key=session_key,
        session_id=session_id,
        document_id=document_id,
    )
    inventory = await _resource_inventory(
        ctx,
        session_key=session_key,
        session_id=session_id,
        service=service,
        include_inline_urls=True,
    )
    resource = next(
        (
            item
            for item in inventory["document"]
            if item["resource"]["id"] == document.document_id
        ),
        None,
    )
    if resource is None:
        raise _internal_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            "materialized document resource is unavailable",
            operation="workbench.resources.open",
        )
    bindings = await service.list_document_source_bindings(
        session_id=session_id,
        limit=1000,
    )
    binding = next(
        (item for item in bindings if item.document_id == document.document_id),
        None,
    )
    response: dict[str, Any] = {
        "disposition": "document",
        "resolution": {"status": "materialized" if materialized else "current"},
        "resource": resource,
        "document": _document_rpc_payload_from_parts(document, head),
        "revision": _revision_payload(head),
        "materialized": materialized,
    }
    if binding is not None:
        response["binding"] = _binding_payload(binding)
    if receipt is not None:
        response["receipt"] = receipt
    return response


def _mutation_resolution_status(status: MutationAttemptStatus) -> str:
    if status is MutationAttemptStatus.APPLIED:
        return "applied"
    if status is MutationAttemptStatus.FAILED:
        return "not_applied"
    return "pending"


async def _mutation_resolution_payload(
    service: ArtifactSessionService,
    *,
    session_key: str,
    session_id: str,
    status: MutationAttemptStatus,
    document_id: str | None,
    revision_id: str | None,
) -> dict[str, Any]:
    """Project internal journals into the intentionally small product wire."""

    public_status = _mutation_resolution_status(status)
    response: dict[str, Any] = {"status": public_status}
    if public_status == "pending":
        response["retryAfterMs"] = _MUTATION_RESOLUTION_RETRY_AFTER_MS
        return response
    if public_status != "applied" or document_id is None:
        return response

    document, head = await _scoped_document(
        service,
        session_key=session_key,
        session_id=session_id,
        document_id=document_id,
    )
    effective_revision = head
    if revision_id is not None:
        try:
            candidate = await service.get_revision(revision_id)
        except ArtifactSessionNotFoundError:
            candidate = head
        if candidate.document_id == document.document_id:
            effective_revision = candidate
    response["document"] = _document_rpc_payload_from_parts(document, head)
    response["result"] = {
        "documentId": document.document_id,
        "revisionId": effective_revision.revision_id,
        "sha256": effective_revision.artifact_sha256,
        "stateRevision": document.state_revision,
    }
    return response


@_d.method("artifacts.mutations.resolve", scope="operator.write")
async def _handle_mutation_resolve(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Resolve a response-loss-safe Artifact mutation without replaying it."""

    session_key, session_id, service = await _scope(params, ctx)
    operation = _require_string(params, "operation", max_bytes=64)
    request_id = _mutation_resolution_request_id(params)

    if operation in _MUTATION_OPERATION_TURN_PREFIX:
        document_id = _optional_string(params, "documentId", max_bytes=256)
        if document_id is None:
            raise artifact_product_error(ArtifactProductErrorCode.INVALID_REQUEST)
        await _scoped_document(
            service,
            session_key=session_key,
            session_id=session_id,
            document_id=document_id,
        )
        turn_id = f"{_MUTATION_OPERATION_TURN_PREFIX[operation]}:{request_id}"
        try:
            mutation_attempt = await service.get_mutation_attempt_for_resolution(
                document_id=document_id,
                turn_id=turn_id,
            )
        except ArtifactSessionNotFoundError:
            return {
                "status": "pending",
                "retryAfterMs": _MUTATION_RESOLUTION_RETRY_AFTER_MS,
            }
        return await _mutation_resolution_payload(
            service,
            session_key=session_key,
            session_id=session_id,
            status=mutation_attempt.status,
            document_id=mutation_attempt.document_id,
            revision_id=mutation_attempt.revision_id,
        )

    if operation in _MUTATION_IMPORT_OPERATIONS:
        try:
            import_attempt = await service.get_document_import_attempt(
                session_id=session_id,
                idempotency_key=request_id,
            )
        except ArtifactSessionNotFoundError:
            return {
                "status": "pending",
                "retryAfterMs": _MUTATION_RESOLUTION_RETRY_AFTER_MS,
            }
        if import_attempt.session_key != session_key:
            raise artifact_product_error(ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE)
        return await _mutation_resolution_payload(
            service,
            session_key=session_key,
            session_id=session_id,
            status=import_attempt.status,
            document_id=import_attempt.document_id,
            revision_id=import_attempt.revision_id,
        )

    if operation in _MUTATION_PUBLISH_OPERATIONS:
        try:
            publish_attempt = await service.get_document_publish_attempt(
                session_id=session_id,
                idempotency_key=request_id,
            )
        except ArtifactSessionNotFoundError:
            return {
                "status": "pending",
                "retryAfterMs": _MUTATION_RESOLUTION_RETRY_AFTER_MS,
            }
        if publish_attempt.session_key != session_key:
            raise artifact_product_error(ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE)
        return await _mutation_resolution_payload(
            service,
            session_key=session_key,
            session_id=session_id,
            status=publish_attempt.status,
            document_id=publish_attempt.document_id,
            revision_id=publish_attempt.revision_id,
        )

    exc = ValueError("unsupported artifact mutation operation")
    raise logged_artifact_product_error(
        ArtifactProductErrorCode.INVALID_REQUEST,
        exc,
        operation="artifacts.mutations.resolve",
        requested_operation=operation,
    ) from None


def _readonly_open_response(resource: dict[str, Any]) -> dict[str, Any]:
    capabilities = resource.get("capabilities")
    reason = "resource_edit_not_supported"
    if isinstance(capabilities, dict):
        candidate = capabilities.get("editReasonCode") or capabilities.get("reasonCode")
        if isinstance(candidate, str) and candidate:
            reason = candidate
    return {
        "disposition": "readonly",
        "resolution": {"status": "readonly"},
        "resource": resource,
        "materialized": False,
        "reasonCode": reason,
    }


def _open_idempotency_key(
    *,
    session_id: str,
    resource_type: str,
    resource_id: str,
    sha256: str,
) -> str:
    identity = "\0".join(
        ("workbench.resources.open", session_id, resource_type, resource_id, sha256)
    )
    return "open-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


@_d.method("workbench.resources.open", scope="operator.write")
async def _handle_resources_open(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Resolve a resource to its current editable Document when supported."""

    session_key, session_id, service = await _scope(params, ctx)
    intent = _optional_string(params, "intent", max_bytes=32) or "edit-current"
    if intent != "edit-current":
        raise ValueError("params.intent must be edit-current")
    expected_sha256 = _optional_string(params, "expectedSha256", max_bytes=64)
    if expected_sha256 is not None:
        expected_sha256 = expected_sha256.lower()
        if _SHA256_RE.fullmatch(expected_sha256) is None:
            raise ValueError("params.expectedSha256 must be a SHA-256 digest")
    requested_idempotency_key = (
        _idempotency_key(params)
        if isinstance(params, dict)
        and ("idempotencyKey" in params or "clientRequestId" in params)
        else None
    )
    resource_type, resource_id = _resource_ref_with_legacy_alias(params)
    if resource_type == "document":
        response = await _current_document_open_response(
            ctx,
            session_key=session_key,
            session_id=session_id,
            service=service,
            document_id=resource_id,
            materialized=False,
        )
        resource_sha256 = response["resource"].get("sha256")
        if expected_sha256 is not None and expected_sha256 != resource_sha256:
            raise _conflict(ArtifactConflictError("document head hash changed"))
        return response

    inventory = await _resource_inventory(
        ctx,
        session_key=session_key,
        session_id=session_id,
        service=service,
        include_inline_urls=True,
    )
    resource = next(
        (
            item
            for item in inventory[resource_type]
            if item["resource"]["id"] == resource_id
        ),
        None,
    )
    if resource is None:
        raise _not_found(resource_type, resource_id)
    resource_sha256 = resource.get("sha256")
    if expected_sha256 is not None and expected_sha256 != resource_sha256:
        raise _conflict(ArtifactConflictError("workbench resource hash changed"))
    if resource_type not in _SUPPORTED_SOURCE_TYPES:
        return _readonly_open_response(resource)

    relations = resource.get("relations")
    related_document_id = (
        relations.get("documentId") if isinstance(relations, dict) else None
    )
    if isinstance(related_document_id, str) and related_document_id:
        return await _current_document_open_response(
            ctx,
            session_key=session_key,
            session_id=session_id,
            service=service,
            document_id=related_document_id,
            materialized=False,
        )

    binding = await service.get_document_source_binding_for_resource(
        session_id=session_id,
        source_type=DocumentSourceType(resource_type),
        source_resource_id=resource_id,
    )
    if binding is not None:
        return await _current_document_open_response(
            ctx,
            session_key=session_key,
            session_id=session_id,
            service=service,
            document_id=binding.document_id,
            materialized=False,
        )

    capabilities = resource.get("capabilities")
    if not isinstance(capabilities, dict) or capabilities.get("manualEdit") is not True:
        return _readonly_open_response(resource)
    sha256 = resource.get("sha256")
    if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
        readonly = _readonly_open_response(resource)
        readonly["reasonCode"] = "resource_digest_unavailable"
        return readonly

    imported = await import_document_from_resource(
        {
            "sessionKey": session_key,
            "source": _resource_ref_payload(resource_type, resource_id),
            "mode": DocumentImportMode.COPY.value,
            "expectedSha256": sha256,
            "idempotencyKey": requested_idempotency_key
            or _open_idempotency_key(
                session_id=session_id,
                resource_type=resource_type,
                resource_id=resource_id,
                sha256=sha256,
            ),
            "name": resource["name"],
        },
        ctx,
    )
    document = imported.get("document")
    document_id = document.get("documentId") if isinstance(document, dict) else None
    if not isinstance(document_id, str) or not document_id:
        raise _internal_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            "materialized document identity is unavailable",
            operation="workbench.resources.open",
        )
    receipt = imported.get("receipt")
    return await _current_document_open_response(
        ctx,
        session_key=session_key,
        session_id=session_id,
        service=service,
        document_id=document_id,
        materialized=True,
        receipt=receipt if isinstance(receipt, dict) else None,
    )


@_d.method("workbench.previews.create", scope="operator.read")
async def _handle_preview_create(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    raw_ref = params.get("resourceRef") if isinstance(params, dict) else None
    if raw_ref is None and isinstance(params, dict):
        raw_ref = params.get("resource")
    projected = dict(params or {})
    projected["resource"] = raw_ref
    resource_type, resource_id = _resource_ref(projected)
    mode = _optional_string(params, "mode", max_bytes=32) or "isolated"
    if mode != "isolated":
        raise ValueError("params.mode must be isolated")
    resource, payload = await _preview_material(
        ctx,
        session_key=session_key,
        session_id=session_id,
        service=service,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    return {
        "resource": resource,
        "preview": _preview_payload(resource, payload, mode=mode),
    }


@_d.method("documents.import", scope="operator.write")
async def _handle_documents_import(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    return await import_document_from_resource(params, ctx)


@_d.method("documents.publish", scope="operator.write")
async def _handle_documents_publish(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(params, ctx)
    document_id = _require_string(params, "documentId", max_bytes=256)
    idempotency_key = _idempotency_key(params)
    requested_name = _optional_string(params, "name", max_bytes=512)
    revision_id = _require_string(params, "revisionId", max_bytes=256)

    try:
        attempt = await service.get_document_publish_attempt(
            session_id=session_id,
            idempotency_key=idempotency_key,
        )
    except ArtifactSessionNotFoundError:
        attempt = None
    replayed = attempt is not None

    if attempt is None:
        document, _head = await _scoped_document(
            service,
            session_key=session_key,
            session_id=session_id,
            document_id=document_id,
        )
        try:
            revision = await service.get_revision(revision_id)
        except ArtifactSessionNotFoundError:
            raise _not_found("revision", revision_id) from None
        if revision.document_id != document.document_id:
            raise _not_found("revision", revision_id)
        profile = _format_profile(revision.filename, revision.media_type)
        if not profile.publishable:
            raise artifact_product_error(
                ArtifactProductErrorCode.RESOURCE_UNSUPPORTED,
                reason_code=profile.reason_code or "format_publish_not_supported",
            )
        candidate = ArtifactBlobRef(
            artifact_id=ArtifactStore.allocate_artifact_id(),
            sha256=revision.artifact_sha256,
            filename=requested_name or document.name,
            media_type=revision.media_type,
            byte_size=revision.byte_size,
        )
        try:
            attempt, created = await service.reserve_document_publish_attempt(
                session_key=session_key,
                session_id=session_id,
                idempotency_key=idempotency_key,
                document_id=document.document_id,
                revision_id=revision.revision_id,
                candidate_artifact=candidate,
            )
        except (ArtifactConflictError, ArtifactValidationError) as exc:
            raise _conflict(exc) from exc
        replayed = not created
    else:
        if (
            attempt.session_key != session_key
            or attempt.document_id != document_id
            or (requested_name is not None and requested_name != attempt.name)
        ):
            raise _conflict(
                ArtifactConflictError(
                    "document publish idempotency key was reused with different input"
                )
            )
        if revision_id != attempt.revision_id:
            raise _conflict(
                ArtifactConflictError(
                    "document publish idempotency key was reused with different revision"
                )
            )
        if attempt.status in {MutationAttemptStatus.FAILED, MutationAttemptStatus.AMBIGUOUS}:
            raise _conflict(
                ArtifactConflictError(
                    f"document publish attempt is terminal: {attempt.status.value}"
                )
            )

    assert attempt is not None
    source_revision = await service.get_revision(attempt.revision_id)
    document, _head = await _scoped_document(
        service,
        session_key=session_key,
        session_id=session_id,
        document_id=attempt.document_id,
    )
    if source_revision.document_id != document.document_id:
        raise _not_found("revision", attempt.revision_id)
    candidate = ArtifactBlobRef(
        artifact_id=attempt.candidate_artifact_id,
        sha256=attempt.artifact_sha256,
        filename=attempt.name,
        media_type=attempt.mime,
        byte_size=attempt.size,
    )
    store = ArtifactStore(media_root_from_config(ctx.config))
    if attempt.status is MutationAttemptStatus.RESERVED:
        try:
            _source_ref, source_path = await asyncio.to_thread(
                store.resolve_for_download,
                source_revision.artifact_id,
                session_id=session_id,
            )
            source_payload = await asyncio.to_thread(native_io_path(source_path).read_bytes)
            await _ensure_internal_candidate(
                ctx,
                session_key=session_key,
                session_id=session_id,
                artifact=candidate,
                payload=source_payload,
                source="document_publish",
            )
            result = await service.apply_document_publish_attempt(
                session_id=session_id,
                idempotency_key=idempotency_key,
                actor=_actor(ctx),
            )
        except (ArtifactConflictError, ArtifactValidationError) as exc:
            try:
                await service.fail_document_publish_attempt(
                    session_id=session_id,
                    idempotency_key=idempotency_key,
                    failure_code="PUBLISH_COMMIT_FAILED",
                )
            except Exception:
                pass
            raise _conflict(exc) from exc
        except (ArtifactNotFoundError, ArtifactIntegrityError, OSError, ValueError) as exc:
            raise logged_artifact_product_error(
                ArtifactProductErrorCode.MUTATION_OUTCOME_PENDING,
                exc,
                operation="documents.publish.prepare",
                accepted=None,
            ) from exc
    else:
        result = await service.apply_document_publish_attempt(
            session_id=session_id,
            idempotency_key=idempotency_key,
            actor=_actor(ctx),
        )

    should_emit = result.attempt.promoted_at is None
    try:
        ref = await asyncio.to_thread(
            store.promote_internal_ref,
            session_id=session_id,
            artifact_id=result.publication.deliverable_artifact_id,
            expected_sha256=result.publication.artifact_sha256,
        )
    except (
        ArtifactError,
        ArtifactIntegrityError,
        ArtifactNotFoundError,
        OSError,
        ValueError,
    ) as exc:
        raise logged_artifact_product_error(
            ArtifactProductErrorCode.MUTATION_OUTCOME_PENDING,
            exc,
            operation="documents.publish.promote",
            accepted=None,
        ) from exc
    try:
        await service.mark_document_publish_promoted(
            session_id=session_id,
            idempotency_key=idempotency_key,
        )
    except (ArtifactConflictError, ArtifactValidationError) as exc:
        raise logged_artifact_product_error(
            ArtifactProductErrorCode.MUTATION_OUTCOME_PENDING,
            exc,
            operation="documents.publish.promote",
            accepted=None,
        ) from exc
    if should_emit:
        await _emit_publish_events(
            ctx,
            session_key=session_key,
            service=service,
            result=result,
            ref=ref,
        )
    return _publish_response(result, ref, replayed=replayed)


__all__ = [
    "_handle_documents_import",
    "_handle_documents_publish",
    "_handle_preview_create",
    "_handle_resources_get",
    "_handle_resources_list",
    "_handle_resources_open",
    "adopt_generated_deliverable_if_editable",
    "import_document_from_resource",
    "resolve_recovery_import_source",
]
