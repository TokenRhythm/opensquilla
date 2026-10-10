"""Materialize an authenticated browser PDF download for existing file readers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextvars
import hashlib
import re
from pathlib import Path

from opensquilla.attachment_workspace import (
    AttachmentWorkspaceMaterializer,
    workspace_attachment_budget_from_config,
)
from opensquilla.tools.browser_attachments import _check_session
from opensquilla.tools.types import ToolContext
from opensquilla.tools.write_policy import attachment_workspace_write_authorizer

MAX_BROWSER_PDF_BYTES = 8 * 1024 * 1024
_MAX_ENCODED_BYTES = 4 * ((MAX_BROWSER_PDF_BYTES + 2) // 3)
_PDF_MIME_TYPES = frozenset({"application/pdf", "application/octet-stream", "binary/octet-stream"})


class BrowserPdfDownloadError(ValueError):
    """A bounded PDF handoff failure without raw bytes or host paths."""


def _validate_pdf_export(export: object, download_id: str) -> bytes:
    if not isinstance(export, dict) or export.get("downloadId") != download_id:
        raise BrowserPdfDownloadError("The PDF download receipt did not match this page.")
    size = export.get("byteLength")
    encoded = export.get("dataBase64")
    sha256 = export.get("sha256")
    mime = export.get("mimeType")
    if (
        export.get("state") != "completed"
        or type(size) is not int
        or not 8 <= size <= MAX_BROWSER_PDF_BYTES
        or not isinstance(encoded, str)
        or len(encoded) > _MAX_ENCODED_BYTES
        or not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or not isinstance(mime, str)
        or mime.split(";", 1)[0].strip().lower() not in _PDF_MIME_TYPES
    ):
        raise BrowserPdfDownloadError("The PDF download metadata was invalid or exceeded 8 MiB.")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise BrowserPdfDownloadError("The PDF download bytes were invalid.") from None
    if (
        len(payload) != size
        or not payload.startswith(b"%PDF-")
        or hashlib.sha256(payload).hexdigest() != sha256
    ):
        raise BrowserPdfDownloadError("The PDF download failed its content integrity check.")
    return payload


async def materialize_browser_pdf_export(
    context: ToolContext,
    download_id: str,
    export: object,
) -> str:
    """Return a session-scoped workspace path readable by the existing pdf tool."""
    if (
        not context.workspace_dir
        or not context.artifact_session_id
        or not context.artifact_media_root
    ):
        raise BrowserPdfDownloadError("This task has no PDF download workspace.")
    config = context.sandbox_gateway_config
    if getattr(getattr(config, "attachments", None), "persist_transcripts", True) is False:
        raise BrowserPdfDownloadError("This task does not retain downloaded files.")
    try:
        await _check_session(context)
    except Exception:
        raise BrowserPdfDownloadError("The PDF download session is no longer available.") from None
    payload = _validate_pdf_export(export, download_id)

    def write() -> str:
        materializer = AttachmentWorkspaceMaterializer(
            media_root=Path(context.artifact_media_root or ""),
            workspace_dir=context.workspace_dir or "",
            materializable_mimes={"application/pdf"},
            disk_budget_bytes=workspace_attachment_budget_from_config(config),
            authorize_write=attachment_workspace_write_authorizer(context),
            working_files=context.attachment_working_files,
        )
        result = materializer.materialize_bytes(
            payload,
            name="browser-download.pdf",
            mime="application/pdf",
            session_id=context.artifact_session_id,
        )
        if not result.available or not result.rel_path:
            raise BrowserPdfDownloadError(
                "The PDF download could not be saved in this task's workspace."
            )
        return result.rel_path

    # Keep physical disk work owned until it settles. An executor Future also
    # survives shutdown cancelling every Task, unlike a to_thread wrapper Task.
    try:
        operation = asyncio.get_running_loop().run_in_executor(
            None, contextvars.copy_context().run, write,
        )
        path = await asyncio.shield(operation)
    except asyncio.CancelledError:
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not operation.cancelled():
            operation.exception()
        raise
    except BrowserPdfDownloadError:
        raise
    except Exception:
        raise BrowserPdfDownloadError(
            "The PDF download could not be saved in this task's workspace."
        ) from None
    try:
        await _check_session(context)
    except Exception:
        raise BrowserPdfDownloadError(
            "The PDF download session changed before it was ready."
        ) from None
    return path
