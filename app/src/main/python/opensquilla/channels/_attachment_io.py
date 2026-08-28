"""Bounded remote attachment reads for channel adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
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
