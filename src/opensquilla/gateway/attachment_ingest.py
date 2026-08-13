"""Shared attachment ingress normalization for RPC and external channels."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog

from opensquilla.attachment_refs import (
    TRANSCRIPT_MATERIAL_STORE,
    PendingChatInputManifestConflictError,
    PendingChatInputManifestCorruptError,
    is_attachment_ref,
    make_attachment_ref,
    make_pending_chat_input_attachment_ref,
    pending_chat_input_manifest_exists,
    read_attachment_ref_bytes,
    read_pending_chat_input_manifest,
    write_pending_chat_input_manifest,
    write_pending_chat_input_material,
    write_transcript_material,
)
from opensquilla.contracts.attachment_sniff import sniff_mime_from_bytes
from opensquilla.contracts.attachments import (
    ALLOWED_MEDIA_TYPES,
    EML_MIME,
    IMAGE_ATTACHMENT_BYTES,
    IMAGE_ATTACHMENT_MIMES,
    INLINE_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    MAX_STAGED_PDF_BYTES,
    MAX_TOTAL_ATTACHMENT_BYTES,
    MBOX_MIME,
    MSG_MIME,
    OFFICE_ATTACHMENT_MIMES,
    OPAQUE_MIME,
    PDF_MAGIC,
    SNIFF_PEEK_BYTES,
    TEXT_ATTACHMENT_BYTES,
    TEXT_ATTACHMENT_MIMES,
    attachment_category,
    attachment_size_limit_for_mime,
    can_stage_attachment_mime,
    normalize_attachment_mime,
)

log = structlog.get_logger(__name__)

__all__ = [
    "ALLOWED_MEDIA_TYPES",
    "IMAGE_ATTACHMENT_BYTES",
    "IMAGE_ATTACHMENT_MIMES",
    "INLINE_ATTACHMENT_BYTES",
    "MAX_ATTACHMENT_BYTES",
    "MAX_ATTACHMENTS",
    "MAX_STAGED_PDF_BYTES",
    "MAX_TOTAL_ATTACHMENT_BYTES",
    "PDF_MAGIC",
    "SNIFF_PEEK_BYTES",
    "TEXT_ATTACHMENT_BYTES",
    "TEXT_ATTACHMENT_MIMES",
    "AttachmentFailure",
    "AttachmentIngestResult",
    "AttachmentResolutionError",
    "AttachmentTotalTooLargeError",
    "ATTACHMENT_EXPIRED_CODE",
    "ATTACHMENT_LOST_IN_RESTART_CODE",
    "attachment_media_type",
    "attachment_size_limit_for_mime",
    "can_stage_attachment_mime",
    "enforce_total_attachment_bytes",
    "ingest_attachments",
    "normalize_attachment_mime",
    "normalize_attachments",
    "resolve_attachments",
    "sniff_mime_from_bytes",
    "stage_pending_chat_input_attachments",
    "validate_attachments",
]


# Typed error codes for staged-attachment resolution failures. They travel on
# the wire so a client can offer a recovery action (re-upload) rather than
# treating the send as a generic, non-retryable INVALID_REQUEST dead end.
ATTACHMENT_EXPIRED_CODE = "ATTACHMENT_EXPIRED"
ATTACHMENT_LOST_IN_RESTART_CODE = "ATTACHMENT_LOST_IN_RESTART"


class AttachmentResolutionError(ValueError):
    """A staged attachment could not be resolved at send time.

    Subclasses :class:`ValueError` so existing ``except ValueError`` handlers
    keep working, but carries a typed ``code`` plus the attachment index and
    uuid so the RPC layer can surface a recoverable, re-uploadable error.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        attachment_index: int,
        file_uuid: str | None,
        recoverable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.attachment_index = attachment_index
        self.file_uuid = file_uuid
        self.recoverable = recoverable


class AttachmentTotalTooLargeError(ValueError):
    pass


@dataclass(frozen=True)
class AttachmentFailure:
    index: int
    name: str
    reason: str
    detail: str

    @property
    def marker(self) -> str:
        return f"[attachment unavailable: {self.name}: {self.reason}]"


@dataclass
class AttachmentIngestResult:
    text: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    failures: list[AttachmentFailure] = field(default_factory=list)
    consumed_file_uuids: list[str] = field(default_factory=list)


def attachment_media_type(attachment: dict[str, Any]) -> str | None:
    """Return the claimed MIME if it names a rendered family, else None."""

    candidates = [
        attachment.get("type"),
        attachment.get("mime"),
        attachment.get("media_type"),
        attachment.get("mime_type"),
    ]
    for candidate in candidates:
        normalized = normalize_attachment_mime(candidate)
        if normalized in ALLOWED_MEDIA_TYPES:
            return normalized
    return None


def _raw_claimed_mime(attachment: dict[str, Any]) -> Any:
    return (
        attachment.get("type")
        or attachment.get("mime")
        or attachment.get("media_type")
        or attachment.get("mime_type")
    )


def _coerce_attachment_dict(attachment: Any) -> dict[str, Any] | None:
    if isinstance(attachment, dict):
        return dict(attachment)
    model_dump = getattr(attachment, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dict(dumped) if isinstance(dumped, dict) else None
    return None


def normalize_attachments(raw_attachments: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_attachments, list):
        return []

    normalized: list[dict[str, Any]] = []
    for attachment in raw_attachments:
        item = _coerce_attachment_dict(attachment)
        if item is None:
            continue
        media_type = attachment_media_type(item)
        if media_type is not None:
            item["type"] = media_type
        normalized.append(item)
    return normalized


def _display_attachment_name(raw: Any, fallback: str) -> str:
    if not isinstance(raw, str):
        return fallback
    collapsed = " ".join(raw.strip().split())
    if not collapsed:
        return fallback
    return collapsed[:160]


def _attachment_name(attachment: dict[str, Any], index: int) -> str:
    raw = attachment.get("name") or attachment.get("filename")
    return _display_attachment_name(raw, f"attachment-{index}")


def _failure(index: int, attachment: dict[str, Any], reason: str, detail: str) -> AttachmentFailure:
    return AttachmentFailure(
        index=index,
        name=_attachment_name(attachment, index),
        reason=reason,
        detail=detail,
    )


def _raise_or_mark(
    *,
    failure_mode: Literal["raise", "mark"],
    failures: list[AttachmentFailure],
    failure: AttachmentFailure,
) -> None:
    if failure_mode == "raise":
        raise ValueError(f"attachments[{failure.index}] {failure.detail}")
    failures.append(failure)


def _raw_bytes_from_data(data: Any, *, index: int) -> tuple[bytes, bool]:
    """Return bytes and whether the source was already bytes instead of base64."""

    if isinstance(data, bytes):
        return data, True
    if isinstance(data, bytearray):
        return bytes(data), True
    if isinstance(data, str) and data:
        try:
            return base64.b64decode(data, validate=True), False
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"attachments[{index}].data must be valid base64") from exc
    raise ValueError(f"attachments[{index}].data is required")


def validate_attachments(
    raw_attachments: Any,
    *,
    failure_mode: Literal["raise", "mark"] = "raise",
    mark_bytes_as_staged: bool = False,
    accept_opaque: bool = True,
    opaque_limit_bytes: int | None = None,
    allow_material_refs: bool = False,
    material_root: Path | None = None,
    expected_material_scope: str | None = None,
    logger: Any | None = None,
) -> tuple[list[dict[str, Any]], list[AttachmentFailure]]:
    normalized = normalize_attachments(raw_attachments)
    failures: list[AttachmentFailure] = []
    if len(normalized) > MAX_ATTACHMENTS:
        failure = AttachmentFailure(
            index=MAX_ATTACHMENTS + 1,
            name="attachments",
            reason="too_many",
            detail=f"supports at most {MAX_ATTACHMENTS} items",
        )
        if failure_mode == "raise":
            raise ValueError(f"attachments supports at most {MAX_ATTACHMENTS} items")
        failures.append(failure)
        normalized = normalized[:MAX_ATTACHMENTS]

    validated: list[dict[str, Any]] = []
    for index, attachment in enumerate(normalized, start=1):
        if attachment.get("_ingest_error"):
            _raise_or_mark(
                failure_mode=failure_mode,
                failures=failures,
                failure=_failure(
                    index,
                    attachment,
                    "download_failed",
                    f"download_failed: {attachment.get('_ingest_error')}",
                ),
            )
            continue

        if is_attachment_ref(attachment):
            if not allow_material_refs:
                _raise_or_mark(
                    failure_mode=failure_mode,
                    failures=failures,
                    failure=_failure(
                        index,
                        attachment,
                        "invalid_shape",
                        "material references are accepted only for internal durable dispatch",
                    ),
                )
                continue
            if material_root is None or not expected_material_scope:
                raise ValueError("material reference validation requires a scoped material root")
            if (
                attachment.get("store") != TRANSCRIPT_MATERIAL_STORE
                or attachment.get("scope") != expected_material_scope
            ):
                _raise_or_mark(
                    failure_mode=failure_mode,
                    failures=failures,
                    failure=_failure(
                        index,
                        attachment,
                        "invalid_material_scope",
                        "material reference belongs to a different session or store",
                    ),
                )
                continue
            try:
                payload = read_attachment_ref_bytes(attachment, media_root=material_root)
            except (OSError, ValueError) as exc:
                _raise_or_mark(
                    failure_mode=failure_mode,
                    failures=failures,
                    failure=_failure(
                        index,
                        attachment,
                        "invalid_material",
                        str(exc),
                    ),
                )
                continue
            media_type = attachment_media_type(attachment)
            if media_type is None:
                media_type = normalize_attachment_mime(_raw_claimed_mime(attachment))
            if media_type is None or (
                not accept_opaque and media_type not in ALLOWED_MEDIA_TYPES
            ):
                _raise_or_mark(
                    failure_mode=failure_mode,
                    failures=failures,
                    failure=_failure(
                        index,
                        attachment,
                        "unsupported_mime",
                        "material reference must declare an allowed media type",
                    ),
                )
                continue
            max_bytes = attachment_size_limit_for_mime(media_type, staged=True)
            if opaque_limit_bytes is not None and attachment_category(media_type) == "opaque":
                max_bytes = min(max_bytes, opaque_limit_bytes)
            if len(payload) > max_bytes:
                _raise_or_mark(
                    failure_mode=failure_mode,
                    failures=failures,
                    failure=_failure(
                        index,
                        attachment,
                        "oversize",
                        f"exceeds the {max_bytes} byte limit",
                    ),
                )
                continue
            item = dict(attachment)
            item["type"] = media_type
            item["mime"] = media_type
            item["name"] = _attachment_name(item, index)
            item["size"] = len(payload)
            validated.append(item)
            continue

        data = attachment.get("data")
        has_data = (isinstance(data, str) and bool(data)) or isinstance(data, (bytes, bytearray))
        file_uuid = attachment.get("file_uuid")
        has_uuid = isinstance(file_uuid, str) and bool(file_uuid)

        if has_data and has_uuid:
            _raise_or_mark(
                failure_mode=failure_mode,
                failures=failures,
                failure=_failure(
                    index,
                    attachment,
                    "invalid_shape",
                    "must carry exactly one of data or file_uuid, not both",
                ),
            )
            continue

        claimed = attachment_media_type(attachment)

        if has_uuid:
            if claimed is not None:
                item = dict(attachment)
                item["type"] = claimed
            else:
                normalized_claim = normalize_attachment_mime(_raw_claimed_mime(attachment))
                if not accept_opaque:
                    _raise_or_mark(
                        failure_mode=failure_mode,
                        failures=failures,
                        failure=_failure(
                            index,
                            attachment,
                            "unsupported_mime",
                            "file_uuid reference must declare a supported mime / media_type",
                        ),
                    )
                    continue
                # Opaque reference: keep the specific label when it normalizes;
                # otherwise the staged upload's own metadata fills the type at
                # resolution, where the bytes are re-validated in full.
                item = dict(attachment)
                if normalized_claim is not None:
                    item["type"] = normalized_claim
                else:
                    item.pop("type", None)
            item["file_uuid"] = file_uuid
            item.pop("data", None)
            validated.append(item)
            continue

        if not has_data:
            _raise_or_mark(
                failure_mode=failure_mode,
                failures=failures,
                failure=_failure(
                    index,
                    attachment,
                    "missing_data",
                    "must carry either data or file_uuid",
                ),
            )
            continue

        try:
            raw_bytes, was_bytes = _raw_bytes_from_data(data, index=index)
        except ValueError as exc:
            _raise_or_mark(
                failure_mode=failure_mode,
                failures=failures,
                failure=_failure(index, attachment, "invalid_data", str(exc)),
            )
            continue

        sniffed = sniff_mime_from_bytes(raw_bytes)

        if claimed is None:
            raw_claim = _raw_claimed_mime(attachment)
            normalized_claim = normalize_attachment_mime(raw_claim)
            if not accept_opaque:
                # Legacy fail-closed admission: only the UTF-8 text fallback is
                # honored; every other unrendered claim is rejected.
                if sniffed == "text/plain":
                    claimed = "text/plain"
                else:
                    _raise_or_mark(
                        failure_mode=failure_mode,
                        failures=failures,
                        failure=_failure(
                            index,
                            attachment,
                            "unsupported_mime",
                            "media type "
                            f"{raw_claim!r} is not allowed; must be one of "
                            f"{sorted(ALLOWED_MEDIA_TYPES)}",
                        ),
                    )
                    continue
            elif sniffed in ALLOWED_MEDIA_TYPES and not (
                sniffed == MSG_MIME and normalized_claim is not None
            ):
                # Authoritative bytes identify a rendered family (an image with
                # an empty claim, unknown-but-textual content, OOXML, email).
                # The one carve-out: OLE magic is shared by legacy Office
                # formats, so a specific non-Outlook claim on an OLE payload is
                # honored as an opaque label instead of misfiling it as email.
                claimed = sniffed
            else:
                # Opaque admission: bytes are never parsed or inlined — the
                # payload lands in the agent workspace only. Keep the specific
                # claim as a label when it normalizes.
                claimed = normalized_claim or OPAQUE_MIME

        if claimed == "application/pdf" and sniffed != "application/pdf":
            _raise_or_mark(
                failure_mode=failure_mode,
                failures=failures,
                failure=_failure(
                    index,
                    attachment,
                    "mime_mismatch",
                    "claims application/pdf but lacks %PDF- magic bytes (415 equivalent)",
                ),
            )
            continue

        if claimed in OFFICE_ATTACHMENT_MIMES and sniffed != claimed:
            _raise_or_mark(
                failure_mode=failure_mode,
                failures=failures,
                failure=_failure(
                    index,
                    attachment,
                    "mime_mismatch",
                    f"claims {claimed} but the bytes are not a matching OOXML "
                    "document (415 equivalent)",
                ),
            )
            continue

        if claimed == MSG_MIME and sniffed != MSG_MIME:
            _raise_or_mark(
                failure_mode=failure_mode,
                failures=failures,
                failure=_failure(
                    index,
                    attachment,
                    "mime_mismatch",
                    "claims an Outlook .msg but is not an OLE compound file "
                    "(415 equivalent)",
                ),
            )
            continue

        # Text-based email (.eml/.mbox) carries the larger email size ceiling, so
        # the bytes must actually look like an email. Otherwise arbitrary text
        # could claim message/rfc822 to bypass the smaller text-attachment cap.
        if claimed in {EML_MIME, MBOX_MIME} and sniffed not in {EML_MIME, MBOX_MIME}:
            _raise_or_mark(
                failure_mode=failure_mode,
                failures=failures,
                failure=_failure(
                    index,
                    attachment,
                    "mime_mismatch",
                    "claims an email message but lacks RFC 5322 / mbox structure "
                    "(415 equivalent)",
                ),
            )
            continue

        if sniffed is not None and sniffed != claimed:
            # The UTF-8 text fallback is a weak signal: never let it downgrade a
            # claimed text-family type (e.g. .csv) we already accept. Larger-cap
            # types (email) are confirmed by the guards above, so by this point a
            # text/plain sniff can only co-occur with a same-cap text claim.
            if sniffed == "text/plain":
                resolved = claimed
            elif sniffed == MSG_MIME and claimed not in ALLOWED_MEDIA_TYPES:
                # OLE magic is shared by legacy Office formats; keep the
                # specific opaque claim (e.g. application/msword) instead of
                # misfiling the payload as Outlook mail.
                resolved = claimed
            else:
                (logger or log).warning(
                    "attachment.mime_mismatch",
                    claimed=claimed,
                    sniffed=sniffed,
                    attachment_index=index,
                )
                resolved = sniffed if sniffed in ALLOWED_MEDIA_TYPES else claimed
        else:
            resolved = claimed

        if (
            attachment_category(resolved) == "text"
            and len(raw_bytes) > TEXT_ATTACHMENT_BYTES
            and sniffed not in ("text/plain", "application/json")
        ):
            # The staged-text ceiling is honored only when the sniffer proved
            # the WHOLE payload is NUL-free UTF-8; otherwise a binary could
            # claim a text mime to shop for the larger cap. Reclassify opaque
            # (or fail closed at the text cap when opaque admission is off).
            if accept_opaque:
                (logger or log).warning(
                    "attachment.text_claim_reclassified_opaque",
                    claimed=resolved,
                    attachment_index=index,
                )
                resolved = OPAQUE_MIME
            else:
                _raise_or_mark(
                    failure_mode=failure_mode,
                    failures=failures,
                    failure=_failure(
                        index,
                        attachment,
                        "oversize",
                        f"exceeds the {TEXT_ATTACHMENT_BYTES} byte limit",
                    ),
                )
                continue

        # Strict deployments keep the legacy stageable set (pdf/image/office),
        # so staged text stays at the 2MB inline cap.
        staged_ok = can_stage_attachment_mime(resolved) and (
            accept_opaque or attachment_category(resolved) in {"pdf", "image", "office"}
        )
        max_bytes = attachment_size_limit_for_mime(
            resolved,
            staged=mark_bytes_as_staged and staged_ok,
        )
        if opaque_limit_bytes is not None and attachment_category(resolved) == "opaque":
            max_bytes = min(max_bytes, opaque_limit_bytes)
        if len(raw_bytes) > max_bytes:
            _raise_or_mark(
                failure_mode=failure_mode,
                failures=failures,
                failure=_failure(
                    index,
                    attachment,
                    "oversize",
                    f"exceeds the {max_bytes} byte limit",
                ),
            )
            continue

        item = dict(attachment)
        item["type"] = resolved
        item["data"] = base64.b64encode(raw_bytes).decode("ascii")
        item["name"] = _attachment_name(item, index)
        item.pop("mime_type", None)
        item.pop("url", None)
        item.pop("size", None)
        item.pop("metadata", None)
        if was_bytes and mark_bytes_as_staged:
            item["_was_staged"] = True
        validated.append(item)

    return validated, failures


def _attachment_raw_size(attachment: dict[str, Any], index: int) -> int:
    if attachment.get("kind") == "attachment_ref":
        size = attachment.get("size")
        if isinstance(size, int) and size >= 0:
            return size
        raise ValueError(f"attachments[{index}].size is required for attachment_ref")
    data = attachment.get("data")
    raw_bytes, _was_bytes = _raw_bytes_from_data(data, index=index)
    return len(raw_bytes)


def enforce_total_attachment_bytes(attachments: list[dict[str, Any]]) -> None:
    total = 0
    for index, attachment in enumerate(attachments, start=1):
        total += _attachment_raw_size(attachment, index)
        if total > MAX_TOTAL_ATTACHMENT_BYTES:
            raise AttachmentTotalTooLargeError(
                "attachments total raw bytes exceed "
                f"the {MAX_TOTAL_ATTACHMENT_BYTES} byte limit"
            )


async def resolve_attachments(
    validated: list[dict[str, Any]],
    store: Any | None = None,
    *,
    material_root: Path | None = None,
    session_id: str | None = None,
    disk_budget_bytes: int | None = None,
    accept_opaque: bool = True,
    opaque_limit_bytes: int | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not any(isinstance(a, dict) and a.get("file_uuid") for a in validated):
        enforce_total_attachment_bytes(validated)
        return validated, []

    from opensquilla.gateway.uploads import (
        AttachmentLostInRestartError,
        AttachmentNotFoundError,
    )
    from opensquilla.gateway.uploads import get_upload_store as _default_store

    upload_store = store if store is not None else _default_store()
    resolved: list[dict[str, Any]] = []
    consumed: list[str] = []
    for index, attachment in enumerate(validated, start=1):
        ref = attachment.get("file_uuid") if isinstance(attachment, dict) else None
        if not isinstance(ref, str):
            resolved.append(attachment)
            continue
        try:
            payload, meta = await upload_store.get(ref)
        except AttachmentLostInRestartError as exc:
            raise AttachmentResolutionError(
                f"attachments[{index}] uuid lost in gateway restart; please re-upload",
                code=ATTACHMENT_LOST_IN_RESTART_CODE,
                attachment_index=index,
                file_uuid=ref,
            ) from exc
        except AttachmentNotFoundError as exc:
            raise AttachmentResolutionError(
                f"attachments[{index}] file_uuid {ref!r} is unknown or expired; please re-upload",
                code=ATTACHMENT_EXPIRED_CODE,
                attachment_index=index,
                file_uuid=ref,
            ) from exc
        candidate = {k: v for k, v in attachment.items() if k != "file_uuid"}
        candidate["data"] = payload
        if "type" not in candidate or not isinstance(candidate.get("type"), str):
            candidate["type"] = meta["mime"]
        if "name" not in candidate or not isinstance(candidate.get("name"), str):
            candidate["name"] = meta["name"]
        materialized, _failures = validate_attachments(
            [candidate],
            failure_mode="raise",
            mark_bytes_as_staged=True,
            accept_opaque=accept_opaque,
            opaque_limit_bytes=opaque_limit_bytes,
        )
        item = materialized[0]
        if material_root is None or not session_id:
            raise ValueError(
                f"attachments[{index}] file_uuid resolution requires a material target"
            )
        raw_bytes, _was_bytes = _raw_bytes_from_data(item.get("data"), index=index)
        sha, _path, _wrote = write_transcript_material(
            media_root=material_root,
            session_id=session_id,
            payload=raw_bytes,
            disk_budget_bytes=disk_budget_bytes,
        )
        resolved.append(
            make_attachment_ref(
                sha256=sha,
                name=item["name"],
                mime=item["type"],
                size=len(raw_bytes),
                session_id=session_id,
                source="upload",
            )
        )
        consumed.append(ref)
    enforce_total_attachment_bytes(resolved)
    return resolved, consumed


async def ingest_attachments(
    text: str,
    raw_attachments: Any,
    *,
    store: Any | None = None,
    failure_mode: Literal["raise", "mark"] = "raise",
    mark_bytes_as_staged: bool = False,
    material_root: Path | None = None,
    session_id: str | None = None,
    disk_budget_bytes: int | None = None,
    accept_opaque: bool = True,
    opaque_limit_bytes: int | None = None,
    allow_material_refs: bool = False,
    expected_material_scope: str | None = None,
) -> AttachmentIngestResult:
    validated, failures = validate_attachments(
        raw_attachments,
        failure_mode=failure_mode,
        mark_bytes_as_staged=mark_bytes_as_staged,
        accept_opaque=accept_opaque,
        opaque_limit_bytes=opaque_limit_bytes,
        allow_material_refs=allow_material_refs,
        material_root=material_root,
        expected_material_scope=expected_material_scope,
    )
    resolved, consumed = await resolve_attachments(
        validated,
        store=store,
        material_root=material_root,
        session_id=session_id,
        disk_budget_bytes=disk_budget_bytes,
        accept_opaque=accept_opaque,
        opaque_limit_bytes=opaque_limit_bytes,
    )
    if failures:
        markers = [failure.marker for failure in failures]
        text = "\n".join([text, *markers]).strip()
    return AttachmentIngestResult(
        text=text,
        attachments=resolved,
        failures=failures,
        consumed_file_uuids=consumed,
    )


async def stage_pending_chat_input_attachments(
    raw_attachments: Any,
    *,
    material_root: Path,
    session_id: str,
    pending_input_id: str,
    enqueue_fingerprint: str,
    store: Any | None = None,
    disk_budget_bytes: int | None = None,
    accept_opaque: bool = True,
    opaque_limit_bytes: int | None = None,
) -> AttachmentIngestResult:
    """Resolve uploads into durable, queue-owned material references.

    This is intentionally separate from normal turn ingestion. A queued item
    must never retain an expiring ``file_uuid`` or inline base64 in SQLite, and
    it cannot place bytes directly in the canonical transcript store because a
    later cancellation must be able to delete only this queue item's material.
    """

    existing_manifest = read_pending_chat_input_manifest(
        media_root=material_root,
        session_id=session_id,
        pending_input_id=pending_input_id,
    )
    if existing_manifest is not None:
        if existing_manifest["enqueue_fingerprint"] != enqueue_fingerprint:
            raise PendingChatInputManifestConflictError(
                "pending input id was already staged for different attachment content"
            )
        return AttachmentIngestResult(
            text="",
            attachments=[dict(item) for item in existing_manifest["attachments"]],
        )
    if pending_chat_input_manifest_exists(
        media_root=material_root,
        session_id=session_id,
        pending_input_id=pending_input_id,
    ):
        # Never overwrite an unreadable recovery record. Doing so could make a
        # conflicting retry take ownership of bytes staged by the original
        # request after a crash.
        raise PendingChatInputManifestCorruptError(
            "pending attachment recovery manifest is invalid"
        )

    validated, _failures = validate_attachments(
        raw_attachments,
        failure_mode="raise",
        mark_bytes_as_staged=True,
        accept_opaque=accept_opaque,
        opaque_limit_bytes=opaque_limit_bytes,
    )
    from opensquilla.gateway.uploads import (
        AttachmentLostInRestartError,
        AttachmentNotFoundError,
    )
    from opensquilla.gateway.uploads import get_upload_store as _default_store

    upload_store = store if store is not None else _default_store()
    refs: list[dict[str, Any]] = []
    consumed: list[str] = []
    for index, attachment in enumerate(validated, start=1):
        file_uuid = attachment.get("file_uuid")
        source = "inline"
        item = attachment
        if isinstance(file_uuid, str) and file_uuid:
            try:
                payload, meta = await upload_store.get(file_uuid)
            except AttachmentLostInRestartError as exc:
                raise AttachmentResolutionError(
                    f"attachments[{index}] uuid lost in gateway restart; please re-upload",
                    code=ATTACHMENT_LOST_IN_RESTART_CODE,
                    attachment_index=index,
                    file_uuid=file_uuid,
                ) from exc
            except AttachmentNotFoundError as exc:
                raise AttachmentResolutionError(
                    f"attachments[{index}] file_uuid {file_uuid!r} is unknown or expired; "
                    "please re-upload",
                    code=ATTACHMENT_EXPIRED_CODE,
                    attachment_index=index,
                    file_uuid=file_uuid,
                ) from exc
            candidate = {k: v for k, v in attachment.items() if k != "file_uuid"}
            candidate["data"] = payload
            if not isinstance(candidate.get("type"), str):
                candidate["type"] = meta["mime"]
            if not isinstance(candidate.get("name"), str):
                candidate["name"] = meta["name"]
            materialized, _ = validate_attachments(
                [candidate],
                failure_mode="raise",
                mark_bytes_as_staged=True,
                accept_opaque=accept_opaque,
                opaque_limit_bytes=opaque_limit_bytes,
            )
            item = materialized[0]
            consumed.append(file_uuid)
            source = "upload"

        raw_bytes, _was_bytes = _raw_bytes_from_data(item.get("data"), index=index)
        sha, _path, _wrote = write_pending_chat_input_material(
            media_root=material_root,
            session_id=session_id,
            pending_input_id=pending_input_id,
            payload=raw_bytes,
            disk_budget_bytes=disk_budget_bytes,
        )
        refs.append(
            make_pending_chat_input_attachment_ref(
                sha256=sha,
                name=_attachment_name(item, index),
                mime=str(item["type"]),
                size=len(raw_bytes),
                session_id=session_id,
                pending_input_id=pending_input_id,
                source=source,
            )
        )

    enforce_total_attachment_bytes(refs)
    write_pending_chat_input_manifest(
        media_root=material_root,
        session_id=session_id,
        pending_input_id=pending_input_id,
        enqueue_fingerprint=enqueue_fingerprint,
        attachments=refs,
    )
    return AttachmentIngestResult(
        text="",
        attachments=refs,
        consumed_file_uuids=consumed,
    )
