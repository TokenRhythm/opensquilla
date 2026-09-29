"""Resolve browser uploads from the current session's retained user attachments."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any

from opensquilla.attachment_refs import transcript_material_path
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.paths import native_io_path
from opensquilla.session.attachment_manifest import (
    MATERIAL_AVAILABLE,
    AttachmentManifest,
    AttachmentManifestStore,
    AttachmentOccurrence,
    normalize_attachment_mime,
    normalize_attachment_name,
    valid_attachment_id,
)
from opensquilla.tools.types import ToolContext

MAX_BROWSER_UPLOAD_BYTES = 8 * 1024 * 1024
_MAX_DESCRIPTORS = 32


class BrowserAttachmentError(ValueError):
    """A public failure that contains no storage path or attachment bytes."""


async def _check_session(context: ToolContext) -> Any:
    manager = context.sandbox_session_manager
    session_id = context.artifact_session_id
    if (
        not manager or not context.session_key or not session_id
        or not context.artifact_media_root or not callable(getattr(manager, "get_session", None))
        or (context.session_id is not None and context.session_id != session_id)
    ):
        raise BrowserAttachmentError("Retained user attachments are unavailable for this task.")
    session = await manager.get_session(context.session_key)
    if (
        session is None or session.session_id != session_id
        or (context.session_epoch is not None and session.epoch != context.session_epoch)
    ):
        raise BrowserAttachmentError("The attachment session changed; start a new task.")
    return manager


async def _manifest(context: ToolContext) -> AttachmentManifest:
    manager = await _check_session(context)
    assert context.session_key is not None
    storage = get_session_storage(manager)
    if storage is None or not callable(getattr(storage, "get_context_states", None)):
        raise BrowserAttachmentError("The user attachment index is unavailable.")
    manifest = await AttachmentManifestStore(storage).load(
        context.session_key, session_id=context.artifact_session_id,
    )
    if manifest.session_id != context.artifact_session_id:
        raise BrowserAttachmentError("The user attachment index has a different owner.")
    return manifest


def _usable(occurrence: AttachmentOccurrence) -> bool:
    return bool(
        occurrence.material_state == MATERIAL_AVAILABLE
        and occurrence.sha256_ref is not None
        and (occurrence.size is None or 0 <= occurrence.size <= MAX_BROWSER_UPLOAD_BYTES)
    )


def _descriptor(occurrence: AttachmentOccurrence) -> dict[str, Any]:
    name = normalize_attachment_name(occurrence.name)
    name = "".join(char for char in name if ord(char) >= 32 and ord(char) != 127)
    if name in {"", ".", ".."}:
        name = "attachment"
    mime = normalize_attachment_mime(occurrence.mime)
    if not re.fullmatch(r"[\w.+-]+/[\w.+-]+", mime, flags=re.ASCII):
        mime = "application/octet-stream"
    return {
        "fileId": occurrence.attachment_id, "name": name, "mimeType": mime,
        **({"size": occurrence.size} if occurrence.size is not None else {}),
    }


def _material_path(context: ToolContext, occurrence: AttachmentOccurrence) -> Path:
    session_id = context.artifact_session_id or ""
    if not session_id or any(char in session_id for char in "/\\\0") or session_id in {".", ".."}:
        raise BrowserAttachmentError("The attachment owner is invalid.")
    root = Path(context.artifact_media_root or "").resolve(strict=True)
    path = native_io_path(transcript_material_path(root, session_id, occurrence.sha256_ref or ""))
    # Reject redirects inside the private store on every supported platform.
    for candidate in (path.parent.parent, path.parent, path):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise BrowserAttachmentError("The retained attachment is no longer available.")
    return path


def _read_material(context: ToolContext, occurrence: AttachmentOccurrence) -> bytes:
    path = _material_path(context, occurrence)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BROWSER_UPLOAD_BYTES:
        raise BrowserAttachmentError("The attachment is not a file within the 8 MiB upload limit.")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev, before.st_ino,
        ) or opened.st_size > MAX_BROWSER_UPLOAD_BYTES:
            raise BrowserAttachmentError("The retained attachment changed while opening.")
        payload = stream.read(MAX_BROWSER_UPLOAD_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (
        len(payload) > MAX_BROWSER_UPLOAD_BYTES
        or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        or (occurrence.size is not None and len(payload) != occurrence.size)
        or hashlib.sha256(payload).hexdigest() != occurrence.sha256_ref
    ):
        raise BrowserAttachmentError("The retained attachment failed its content integrity check.")
    return payload


async def browser_upload_descriptors(context: ToolContext) -> list[dict[str, Any]]:
    """List bounded, path-free IDs; this does not read any attachment content."""
    try:
        manifest = await _manifest(context)
    except Exception:
        # Optional attachment discovery must not break an otherwise usable page
        # when this installation cannot expose its retained attachment index.
        return []
    items: list[dict[str, Any]] = []
    for item in reversed(manifest.occurrences):
        if _usable(item):
            items.append(_descriptor(item))
            if len(items) == _MAX_DESCRIPTORS:
                break
    return list(reversed(items))


async def resolve_browser_upload(context: ToolContext, file_id: object) -> dict[str, Any]:
    """Load only an exact, session-owned occurrence selected by its opaque ID."""
    attachment_id = valid_attachment_id(file_id)
    if attachment_id is None:
        raise BrowserAttachmentError("Use a user attachment fileId from availableUploads.")
    try:
        manifest = await _manifest(context)
        occurrence = manifest.by_id(attachment_id)
        if occurrence is None or not _usable(occurrence):
            raise BrowserAttachmentError(
                "The attachment is unavailable in this session or exceeds the 8 MiB upload limit."
            )
        payload = await asyncio.to_thread(_read_material, context, occurrence)
        await _check_session(context)
    except BrowserAttachmentError:
        raise
    except Exception:
        raise BrowserAttachmentError("The retained user attachment could not be read.") from None
    result = _descriptor(occurrence)
    result.pop("size", None)
    result["dataBase64"] = base64.b64encode(payload).decode("ascii")
    return result
