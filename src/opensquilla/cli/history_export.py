"""Bounded-memory Gateway transcript exports.

Gateway history pages are newest-first while transcript files are chronological.
This module spools each bounded page to disk, then copies the page files in
reverse page order into one atomically replaced destination. Oversized v2
entries remain chunked throughout the export.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from opensquilla.cli.chat.session_state import messages_to_markdown
from opensquilla.cli.gateway_client import GatewayRPCError

_DETAIL_METHOD = "chat.history.entry.v1"
_DETAIL_CHUNK_BYTES = 128 * 1024
_COPY_CHUNK_BYTES = 128 * 1024
_ExportMode = Literal["json", "markdown"]


class HistoryExportClient(Protocol):
    """Gateway operations required by the streaming exporter."""

    async def session_history(
        self,
        session_key: str,
        limit: int = 1000,
        *,
        before: str | None = None,
        after: str | None = None,
        include_canonical: bool | None = None,
        include_summaries: bool | None = None,
    ) -> dict[str, Any]: ...

    async def call(self, method: str, params: dict | None = None) -> Any: ...


@dataclass(frozen=True)
class HistoryExportResult:
    """Small export receipt that never contains transcript messages."""

    target: Path
    message_count: int
    page_count: int


@dataclass(frozen=True)
class _SpooledHistory:
    root: Path
    page_count: int
    message_count: int
    output_count: int
    metadata: dict[str, Any]


def _history_message_identity(message: dict[str, Any]) -> tuple[str, str] | None:
    transcript_id = message.get("transcript_id")
    if transcript_id not in (None, ""):
        return "transcript", str(transcript_id)
    message_id = message.get("message_id") or message.get("id")
    if message_id not in (None, ""):
        return "message", str(message_id)
    return None


def _history_cursor_key(value: object) -> tuple[int, int] | None:
    raw = str(value or "").strip()
    if not raw or "|" not in raw:
        return None
    created_at, transcript_id = raw.split("|", 1)
    try:
        return int(created_at), int(transcript_id)
    except ValueError:
        return None


def _export_error(code: str, message: str, *, method: str = "chat.history") -> GatewayRPCError:
    return GatewayRPCError(method, code=code, message=message)


def _write_error(exc: OSError) -> GatewayRPCError:
    return _export_error(
        "TRANSCRIPT_EXPORT_WRITE_FAILED",
        f"could not write transcript export: {exc}",
        method="sessions.export",
    )


def _marker_path(root: Path, namespace: str, value: str) -> Path:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return root / namespace / digest


def _mark_once(root: Path, namespace: str, value: str) -> bool:
    marker = _marker_path(root, namespace, value)
    marker.parent.mkdir(exist_ok=True)
    try:
        marker.touch(exist_ok=False)
    except FileExistsError:
        return False
    return True


def _role_heading(message: dict[str, Any]) -> str:
    role = str(message.get("role") or "message")
    if role == "user":
        return "You"
    if role == "assistant":
        return "Assistant"
    return role.title()


def _detail_reference(
    message: dict[str, Any],
    *,
    session_key: str,
) -> tuple[str, dict[str, Any]] | None:
    raw = message.get("detail_ref")
    if raw is None:
        if message.get("truncated_by_bytes") is True:
            raise _export_error(
                "HISTORY_DETAIL_REFERENCE_MISSING",
                "gateway returned a truncated history preview without a detail reference",
            )
        return None
    if not isinstance(raw, dict):
        raise _export_error(
            "INVALID_HISTORY_DETAIL_REF",
            "gateway returned a non-object history detail reference",
        )
    method = str(raw.get("method") or "").strip()
    if method != _DETAIL_METHOD:
        raise _export_error(
            "INVALID_HISTORY_DETAIL_REF",
            "gateway returned an unsupported history detail method",
        )
    ref_session_key = str(raw.get("sessionKey") or "").strip()
    cursor = str(raw.get("cursor") or "").strip()
    if ref_session_key != session_key or _history_cursor_key(cursor) is None:
        raise _export_error(
            "INVALID_HISTORY_DETAIL_REF",
            "gateway returned an invalid history detail identity",
        )
    return method, {"sessionKey": ref_session_key, "cursor": cursor}


def _chunk_int(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _export_error(
            "INVALID_HISTORY_DETAIL_CHUNK",
            f"gateway history detail chunk had an invalid {name}",
            method=_DETAIL_METHOD,
        )
    return value


async def _write_detail_chunks(
    client: HistoryExportClient,
    *,
    method: str,
    reference: dict[str, Any],
    field: Literal["message", "text"],
    write: Callable[[bytes], Any],
    declared_total: int | None = None,
) -> None:
    offset = 0
    expected_total: int | None = None
    expected_digest: str | None = None
    digest = hashlib.sha256()

    while True:
        payload = await client.call(
            method,
            {
                **reference,
                "field": field,
                "offset": offset,
                "chunkBytes": _DETAIL_CHUNK_BYTES,
            },
        )
        if not isinstance(payload, dict):
            raise _export_error(
                "INVALID_HISTORY_DETAIL_CHUNK",
                "gateway returned a non-object history detail chunk",
                method=method,
            )
        if payload.get("encoding") != "base64" or payload.get("field") != field:
            raise _export_error(
                "INVALID_HISTORY_DETAIL_CHUNK",
                "gateway history detail chunk changed encoding or field",
                method=method,
            )
        chunk_offset = _chunk_int(payload, "offset")
        total = _chunk_int(payload, "total")
        if chunk_offset != offset:
            raise _export_error(
                "HISTORY_DETAIL_OFFSET_MISMATCH",
                "gateway history detail chunk did not start at the requested offset",
                method=method,
            )

        raw_digest = payload.get("sha256")
        if not isinstance(raw_digest, str) or len(raw_digest) != 64:
            raise _export_error(
                "INVALID_HISTORY_DETAIL_CHUNK",
                "gateway history detail chunk omitted its SHA-256 digest",
                method=method,
            )
        try:
            int(raw_digest, 16)
        except ValueError as exc:
            raise _export_error(
                "INVALID_HISTORY_DETAIL_CHUNK",
                "gateway history detail chunk returned an invalid SHA-256 digest",
                method=method,
            ) from exc
        if expected_total is None:
            if declared_total is not None and total != declared_total:
                raise _export_error(
                    "HISTORY_DETAIL_LENGTH_MISMATCH",
                    "gateway history detail did not match its declared original byte length",
                    method=method,
                )
            expected_total = total
            expected_digest = raw_digest.lower()
        elif total != expected_total or raw_digest.lower() != expected_digest:
            raise _export_error(
                "HISTORY_DETAIL_IDENTITY_CHANGED",
                "gateway history detail changed while it was being exported",
                method=method,
            )

        raw_chunk = payload.get("chunk_base64")
        if not isinstance(raw_chunk, str):
            raise _export_error(
                "INVALID_HISTORY_DETAIL_CHUNK",
                "gateway history detail chunk omitted base64 content",
                method=method,
            )
        try:
            chunk = base64.b64decode(raw_chunk, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _export_error(
                "INVALID_HISTORY_DETAIL_CHUNK",
                "gateway history detail chunk contained invalid base64 content",
                method=method,
            ) from exc

        end = offset + len(chunk)
        if expected_total is None or end > expected_total:
            raise _export_error(
                "INVALID_HISTORY_DETAIL_CHUNK",
                "gateway history detail chunk exceeded its declared byte length",
                method=method,
            )
        write(chunk)
        digest.update(chunk)

        next_offset = payload.get("next")
        if next_offset is None:
            if end != expected_total:
                raise _export_error(
                    "HISTORY_DETAIL_INCOMPLETE",
                    "gateway history detail ended before its declared byte length",
                    method=method,
                )
            break
        if (
            isinstance(next_offset, bool)
            or not isinstance(next_offset, int)
            or next_offset != end
            or next_offset <= offset
            or next_offset >= expected_total
        ):
            raise _export_error(
                "HISTORY_DETAIL_PAGINATION_STALLED",
                "gateway history detail offset did not advance",
                method=method,
            )
        offset = next_offset

    if expected_digest is None or digest.hexdigest() != expected_digest:
        raise _export_error(
            "HISTORY_DETAIL_CHECKSUM_MISMATCH",
            "gateway history detail failed its SHA-256 integrity check",
            method=method,
        )


async def _spool_message(
    client: HistoryExportClient,
    message: dict[str, Any],
    *,
    session_key: str,
    mode: _ExportMode,
    target: Path,
) -> None:
    detail = _detail_reference(message, session_key=session_key)
    with target.open("wb") as stream:
        if mode == "json":
            if detail is None:
                stream.write(
                    json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
                return
            method, reference = detail
            original_bytes = message.get("original_bytes")
            declared_total = (
                original_bytes
                if isinstance(original_bytes, int)
                and not isinstance(original_bytes, bool)
                and original_bytes >= 0
                else None
            )
            await _write_detail_chunks(
                client,
                method=method,
                reference=reference,
                field="message",
                write=stream.write,
                declared_total=declared_total,
            )
            return

        if detail is None:
            stream.write(messages_to_markdown([message]).encode("utf-8"))
            return
        method, reference = detail
        stream.write(f"## {_role_heading(message)}\n\n".encode())
        await _write_detail_chunks(
            client,
            method=method,
            reference=reference,
            field="text",
            write=stream.write,
        )
        stream.write(b"\n")


def _page_message_paths(root: Path, page_index: int) -> list[Path]:
    page = root / f"page-{page_index:08d}"
    return sorted(page.glob("message-*.data"))


async def _spool_page_messages(
    client: HistoryExportClient,
    raw_messages: list[Any],
    *,
    session_key: str,
    root: Path,
    page_index: int,
    mode: _ExportMode,
) -> tuple[int, int]:
    page_path = root / f"page-{page_index:08d}"
    page_path.mkdir()
    message_count = 0
    output_count = 0
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            continue
        message = dict(raw_message)
        identity = _history_message_identity(message)
        if identity is not None:
            identity_marker = json.dumps(identity, separators=(",", ":"))
            if not _mark_once(root, "identities", identity_marker):
                continue
        message_path = page_path / f"message-{output_count:08d}.data"
        await _spool_message(
            client,
            message,
            session_key=session_key,
            mode=mode,
            target=message_path,
        )
        message_count += 1
        if mode == "markdown" and message_path.stat().st_size == 0:
            message_path.unlink()
            continue
        output_count += 1
    return message_count, output_count


async def _spool_history(
    client: HistoryExportClient,
    session_key: str,
    *,
    root: Path,
    mode: _ExportMode,
    page_size: int,
) -> _SpooledHistory:
    limit = max(1, min(int(page_size), 200))
    before: str | None = None
    page_count = 0
    message_count = 0
    output_count = 0
    newest_metadata: dict[str, Any] | None = None
    oldest_metadata: dict[str, Any] | None = None

    while True:
        response = await client.session_history(
            session_key,
            limit=limit,
            before=before,
            include_canonical=True,
            include_summaries=False,
        )
        if not isinstance(response, dict):
            raise _export_error(
                "INVALID_HISTORY_PAGE",
                "gateway returned a non-object history page",
            )
        if response.get("canonical_available") is False:
            raise _export_error(
                "CANONICAL_HISTORY_UNAVAILABLE",
                "complete canonical history is temporarily unavailable; export was cancelled",
            )
        if response.get("canonical_complete") is False:
            raise _export_error(
                "CANONICAL_HISTORY_INCOMPLETE",
                "older original messages were not preserved; export was cancelled",
            )
        raw_messages = response.get("messages")
        if not isinstance(raw_messages, list):
            raise _export_error(
                "INVALID_HISTORY_PAGE",
                "gateway history page did not contain a messages list",
            )

        has_more = bool(response.get("has_more"))
        next_before: str | None = None
        if has_more:
            raw_cursor = response.get("oldest_cursor")
            next_before = str(raw_cursor).strip() if raw_cursor is not None else ""
            if not next_before or next_before == before:
                raise _export_error(
                    "HISTORY_PAGINATION_STALLED",
                    "gateway history cursor did not advance; export was cancelled",
                )
            if not _mark_once(root, "cursors", next_before):
                raise _export_error(
                    "HISTORY_PAGINATION_STALLED",
                    "gateway history cursor did not advance; export was cancelled",
                )
        if before is not None:
            requested_key = _history_cursor_key(before)
            newest_key = _history_cursor_key(response.get("newest_cursor"))
            if requested_key is None or newest_key is None or newest_key >= requested_key:
                raise _export_error(
                    "HISTORY_CURSOR_INVALIDATED",
                    (
                        "gateway history no longer precedes the requested cursor; "
                        "the session may have changed and export was cancelled"
                    ),
                )

        page_message_count, page_output_count = await _spool_page_messages(
            client,
            raw_messages,
            session_key=session_key,
            root=root,
            page_index=page_count,
            mode=mode,
        )
        message_count += page_message_count
        output_count += page_output_count

        metadata = {key: value for key, value in response.items() if key != "messages"}
        newest_metadata = newest_metadata or metadata
        oldest_metadata = metadata
        # Do not retain a completed page while awaiting the next RPC response.
        del raw_messages
        response = {}
        page_count += 1
        if not has_more:
            break
        assert next_before is not None
        before = next_before

    result_metadata = dict(oldest_metadata or newest_metadata or {})
    result_metadata["has_more"] = False
    result_metadata["loaded_count"] = message_count
    result_metadata["page_size"] = limit
    result_metadata["truncated_by_bytes"] = False
    result_metadata.pop("wire_bytes", None)
    result_metadata.pop("byte_budget", None)
    if newest_metadata is not None:
        result_metadata["newest_cursor"] = newest_metadata.get("newest_cursor")
    return _SpooledHistory(
        root=root,
        page_count=page_count,
        message_count=message_count,
        output_count=output_count,
        metadata=result_metadata,
    )


def _copy_file(source: Path, target: Any) -> None:
    with source.open("rb") as stream:
        shutil.copyfileobj(stream, target, length=_COPY_CHUNK_BYTES)


def _write_atomic(target: Path, writer: Callable[[Any], None]) -> None:
    target = target.expanduser()
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        temporary_path = Path(raw_path)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            writer(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    except OSError as exc:
        raise _write_error(exc) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _write_json_export(
    stream: Any,
    *,
    spooled: _SpooledHistory,
    resolved: dict[str, Any],
    preview: dict[str, Any],
) -> None:
    stream.write(b'{\n  "resolved": ')
    stream.write(json.dumps(resolved, ensure_ascii=False, indent=2).encode("utf-8"))
    stream.write(b',\n  "preview": ')
    stream.write(json.dumps(preview, ensure_ascii=False, indent=2).encode("utf-8"))
    stream.write(b',\n  "history": {')
    for key, value in spooled.metadata.items():
        stream.write(b"\n    ")
        stream.write(json.dumps(str(key), ensure_ascii=False).encode("utf-8"))
        stream.write(b": ")
        stream.write(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        stream.write(b",")
    stream.write(b'\n    "messages": [')

    wrote_message = False
    for page_index in range(spooled.page_count - 1, -1, -1):
        for path in _page_message_paths(spooled.root, page_index):
            if wrote_message:
                stream.write(b",")
            stream.write(b"\n      ")
            _copy_file(path, stream)
            wrote_message = True
    if wrote_message:
        stream.write(b"\n    ")
    stream.write(b"]\n  }\n}\n")


def _write_markdown_export(
    stream: Any,
    *,
    spooled: _SpooledHistory,
    header: str,
    fallback_markdown: str,
) -> None:
    if header:
        stream.write(header.encode("utf-8"))
    if not spooled.output_count:
        stream.write(fallback_markdown.encode("utf-8"))
        return

    wrote_message = False
    for page_index in range(spooled.page_count - 1, -1, -1):
        for path in _page_message_paths(spooled.root, page_index):
            if wrote_message:
                stream.write(b"\n")
            _copy_file(path, stream)
            wrote_message = True


async def export_session_history_json(
    client: HistoryExportClient,
    session_key: str,
    target: Path,
    *,
    resolved: dict[str, Any],
    preview: dict[str, Any],
    page_size: int = 200,
) -> HistoryExportResult:
    """Stream a complete chronological history into the legacy JSON export shape."""

    try:
        with tempfile.TemporaryDirectory(prefix="opensquilla-history-export-") as raw_root:
            spooled = await _spool_history(
                client,
                session_key,
                root=Path(raw_root),
                mode="json",
                page_size=page_size,
            )
            _write_atomic(
                target,
                lambda stream: _write_json_export(
                    stream,
                    spooled=spooled,
                    resolved=resolved,
                    preview=preview,
                ),
            )
            return HistoryExportResult(
                target=target,
                message_count=spooled.message_count,
                page_count=spooled.page_count,
            )
    except OSError as exc:
        if isinstance(exc, ConnectionError):
            raise
        raise _write_error(exc) from exc


async def export_session_history_markdown(
    client: HistoryExportClient,
    session_key: str,
    target: Path,
    *,
    header: str = "",
    fallback_markdown: str = "",
    page_size: int = 200,
) -> HistoryExportResult:
    """Stream a complete chronological history into a Markdown transcript."""

    try:
        with tempfile.TemporaryDirectory(prefix="opensquilla-history-export-") as raw_root:
            spooled = await _spool_history(
                client,
                session_key,
                root=Path(raw_root),
                mode="markdown",
                page_size=page_size,
            )
            _write_atomic(
                target,
                lambda stream: _write_markdown_export(
                    stream,
                    spooled=spooled,
                    header=header,
                    fallback_markdown=fallback_markdown,
                ),
            )
            return HistoryExportResult(
                target=target,
                message_count=spooled.message_count,
                page_count=spooled.page_count,
            )
    except OSError as exc:
        if isinstance(exc, ConnectionError):
            raise
        raise _write_error(exc) from exc


__all__ = [
    "HistoryExportClient",
    "HistoryExportResult",
    "export_session_history_json",
    "export_session_history_markdown",
]
