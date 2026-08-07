"""Bounded remote attachment reads for channel adapters."""

from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from opensquilla.contracts.attachments import (
    ALLOWED_MEDIA_TYPES,
    MAX_ATTACHMENT_BYTES,
    attachment_size_limit_for_mime,
    can_stage_attachment_mime,
    normalize_attachment_mime,
)

_CHUNK_BYTES = 64 * 1024


class RemoteAttachmentTooLargeError(ValueError):
    """Raised before a channel adapter materializes an oversized remote file."""


def _display_name(name: str | None) -> str:
    return name or "attachment"


def _too_large(name: str | None, limit: int) -> RemoteAttachmentTooLargeError:
    return RemoteAttachmentTooLargeError(
        f"{_display_name(name)} exceeds the {limit} byte attachment limit"
    )


def attachment_limit_for_mime(mime: str | None) -> int:
    # Channel downloads feed the staged ingest path, so every normalizable
    # type gets its staged ceiling (opaque included); email stays at the
    # inline text cap because it is never stageable.
    normalized = normalize_attachment_mime(mime)
    if normalized is None:
        return MAX_ATTACHMENT_BYTES
    return attachment_size_limit_for_mime(
        normalized, staged=can_stage_attachment_mime(normalized)
    )


def ensure_declared_size_within_limit(
    size: Any,
    *,
    name: str | None,
    limit: int = MAX_ATTACHMENT_BYTES,
) -> None:
    if isinstance(size, int) and size > limit:
        raise _too_large(name, limit)


def ensure_bytes_within_limit(
    payload: bytes | bytearray,
    *,
    name: str | None,
    limit: int = MAX_ATTACHMENT_BYTES,
) -> bytes:
    if len(payload) > limit:
        raise _too_large(name, limit)
    return bytes(payload)


def _header_value(headers: Any, key: str) -> str | None:
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(key) or getter(key.lower()) or getter(key.title())
        return value if isinstance(value, str) else None
    if isinstance(headers, dict):
        value = headers.get(key) or headers.get(key.lower()) or headers.get(key.title())
        return value if isinstance(value, str) else None
    return None


def content_type_from_headers(headers: Any) -> str | None:
    raw = _header_value(headers, "content-type")
    return raw.split(";", 1)[0].strip() if raw else None


async def materialize_attachment(attachment: Any) -> Path:
    """Materialize one ``Attachment`` to a local temp file for upload.

    Prefers the in-memory ``data`` bytes; falls back to downloading ``url``
    with the same size bound used for inbound channel attachments. The caller
    owns the returned path and must unlink it.
    """

    data = getattr(attachment, "data", None)
    if isinstance(data, (bytes, bytearray)):
        payload = ensure_bytes_within_limit(data, name=getattr(attachment, "name", None))
        return _write_temp(payload, name=getattr(attachment, "name", None))

    url = getattr(attachment, "url", None)
    if not isinstance(url, str) or not url:
        raise ValueError("attachment requires either data bytes or a URL")
    name = getattr(attachment, "name", None)
    declared_size = getattr(attachment, "size", None)
    ensure_declared_size_within_limit(declared_size, name=name)

    import httpx

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=10.0),
    ) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            limit = attachment_limit_for_mime(
                getattr(attachment, "mime_type", None) or content_type_from_headers(resp.headers)
            )
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes(_CHUNK_BYTES):
                total += len(chunk)
                if total > limit:
                    raise RemoteAttachmentTooLargeError(
                        f"{name or 'attachment'} exceeds the {limit} byte attachment limit"
                    )
                chunks.append(chunk)
    return _write_temp(b"".join(chunks), name=name)


async def download_attachment_bytes(
    attachment: Any,
    *,
    client: Any = None,
) -> bytes:
    """Download an attachment URL into bytes, streaming with the size bound.

    Shared by channel ``resolve_inbound_attachment`` implementations so every
    adapter applies the same size limit while downloading (instead of reading
    the full body into memory first). Accepts an optional httpx client so an
    adapter can reuse its authenticated client (e.g. Slack's bot token).
    """

    url = str(getattr(attachment, "url", "") or "").strip()
    if not url:
        raise ValueError("attachment requires a URL")
    name = getattr(attachment, "name", None)
    declared_size = getattr(attachment, "size", None)
    ensure_declared_size_within_limit(declared_size, name=name)
    limit = attachment_limit_for_mime(getattr(attachment, "mime_type", None))

    import httpx

    async def _stream(stream_client: Any) -> bytes:
        async with stream_client.stream("GET", url) as resp:
            resp.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes(_CHUNK_BYTES):
                total += len(chunk)
                if total > limit:
                    raise RemoteAttachmentTooLargeError(
                        f"{name or 'attachment'} exceeds the {limit} byte attachment limit"
                    )
                chunks.append(chunk)
        return b"".join(chunks)

    if client is not None:
        return await _stream(client)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=10.0),
    ) as client:
        return await _stream(client)


def _write_temp(payload: bytes, *, name: str | None) -> Path:
    """Write bytes to a temp file with a sane suffix from the attachment name."""

    suffix = Path(Path(name or "").name).suffix.lower()[:16] if name else ""
    fd, raw_path = tempfile.mkstemp(prefix="opensquilla-channel-", suffix=suffix)
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


async def deliver_message_attachments(
    channel: Any,
    *,
    target: str,
    content: str,
    attachments: list[Any],
    **send_kwargs: Any,
) -> None:
    """Deliver every attachment on an outgoing message via ``channel.send_file``.

    Best-effort per attachment: an unsupported channel raises
    ``UnsupportedChannelOperation`` (the caller may degrade to a text notice),
    and a transient send failure propagates so the outbox/retry layer can
    redeliver the whole message.
    """

    for attachment in attachments:
        path = await materialize_attachment(attachment)
        try:
            send = channel.send_file
            sig = inspect.signature(send)
            call_kwargs: dict[str, Any] = {}
            if "content" in sig.parameters:
                call_kwargs["content"] = content
            call_kwargs.update(send_kwargs)
            attachment_name = getattr(attachment, "name", None)
            if "file_name" in sig.parameters and isinstance(attachment_name, str) and attachment_name:
                call_kwargs["file_name"] = attachment_name
            send_result = send(target, str(path), **call_kwargs)
            if asyncio.iscoroutine(send_result):
                await send_result
        finally:
            await asyncio.to_thread(path.unlink, missing_ok=True)


def preferred_attachment_mime(downloaded: str | None, declared: str | None) -> str | None:
    """Prefer a downloaded MIME only when it is in the attachment allow-list."""

    if isinstance(downloaded, str):
        downloaded = downloaded.split(";", 1)[0].strip()
        if downloaded in ALLOWED_MEDIA_TYPES:
            return downloaded
    if isinstance(declared, str) and declared in ALLOWED_MEDIA_TYPES:
        return declared
    return downloaded or declared


def ensure_content_length_within_limit(
    headers: Any,
    *,
    name: str | None,
    limit: int = MAX_ATTACHMENT_BYTES,
) -> None:
    raw = _header_value(headers, "content-length")
    if not raw:
        return
    try:
        content_length = int(raw)
    except ValueError:
        return
    if content_length > limit:
        raise _too_large(name, limit)


async def read_limited_chunks(
    chunks: AsyncIterator[bytes],
    *,
    name: str | None,
    limit: int = MAX_ATTACHMENT_BYTES,
) -> bytes:
    parts: list[bytes] = []
    total = 0
    async for chunk in chunks:
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise _too_large(name, limit)
        parts.append(bytes(chunk))
    return b"".join(parts)


async def fetch_httpx_bytes_limited(
    client: Any,
    url: str,
    *,
    name: str | None,
    limit: int = MAX_ATTACHMENT_BYTES,
    **request_kwargs: Any,
) -> tuple[bytes, str | None]:
    async with client.stream("GET", url, **request_kwargs) as response:
        response.raise_for_status()
        ensure_content_length_within_limit(response.headers, name=name, limit=limit)
        payload = await read_limited_chunks(
            response.aiter_bytes(),
            name=name,
            limit=limit,
        )
        return payload, content_type_from_headers(response.headers)


async def read_aiohttp_response_bytes_limited(
    response: Any,
    *,
    name: str | None,
    limit: int = MAX_ATTACHMENT_BYTES,
) -> tuple[bytes, str | None]:
    status = getattr(response, "status", None)
    if isinstance(status, int) and status >= 400:
        raise RuntimeError(f"attachment download failed with HTTP {status}")

    headers = getattr(response, "headers", {})
    ensure_content_length_within_limit(headers, name=name, limit=limit)
    content = getattr(response, "content", None)
    iter_chunked = getattr(content, "iter_chunked", None)
    if callable(iter_chunked):
        payload = await read_limited_chunks(
            iter_chunked(_CHUNK_BYTES),
            name=name,
            limit=limit,
        )
        return payload, content_type_from_headers(headers)

    read = getattr(response, "read", None)
    if not callable(read):
        raise RuntimeError("attachment download returned no readable body")
    payload = ensure_bytes_within_limit(await read(), name=name, limit=limit)
    return payload, content_type_from_headers(headers)
