"""Bounded, read-only access to legacy transcript content.

The legacy transcript keeps message bodies in SQLite ``TEXT`` columns.  A
history page should therefore carry identity and preview metadata only; a
caller that needs the body reads a finite byte range through this port.  The
port is deliberately transport and persistence neutral so the same contract
can later back the Gateway HTTP ``content.read.v1`` endpoint.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from opensquilla.content_reader import (
    MAX_CONTENT_RANGE_BYTES,
    MAX_DISPLAY_CONTENT_BYTES,
    ContentEncodingError,
    ContentMetadataPendingError,
    ContentNotFoundError,
    ContentRange,
    ContentRangeError,
    ContentReadError,
    ContentSource,
    LegacyContentRef,
    validate_content_range,
)

MAX_CONTENT_EXPORT_BYTES = 8 * 1024 * 1024


class ContentExportLimitError(ContentReadError):
    """The caller requested a full export larger than the bounded export cap."""


class ContentReader(Protocol):
    """Port implemented by storage adapters and used by transport handlers."""

    async def get_ref(
        self,
        session_id: str,
        message_id: str,
        *,
        source: ContentSource | None = None,
    ) -> LegacyContentRef: ...

    async def read_range(
        self,
        ref: LegacyContentRef,
        *,
        offset: int = 0,
        limit: int = MAX_CONTENT_RANGE_BYTES,
    ) -> ContentRange: ...

    async def read_display_text(
        self,
        ref: LegacyContentRef,
        *,
        max_bytes: int = MAX_DISPLAY_CONTENT_BYTES,
    ) -> str: ...


async def iter_content_ranges(
    reader: ContentReader,
    ref: LegacyContentRef,
    *,
    chunk_bytes: int = MAX_CONTENT_RANGE_BYTES,
) -> AsyncIterator[ContentRange]:
    """Iterate a body through bounded reads, never loading it all at once."""

    _, chunk_bytes = validate_content_range(0, chunk_bytes)
    offset = 0
    while offset < ref.byte_length:
        chunk = await reader.read_range(ref, offset=offset, limit=chunk_bytes)
        if chunk.offset != offset:
            raise ContentReadError("content reader returned a non-contiguous range")
        if not chunk.data:
            raise ContentReadError("content reader returned an empty non-terminal range")
        yield chunk
        offset = chunk.end
    if ref.byte_length == 0:
        # Empty legacy bodies still have a well-defined, observable range.
        yield await reader.read_range(ref, offset=0, limit=chunk_bytes)


async def read_utf8_text(
    reader: ContentReader,
    ref: LegacyContentRef,
    *,
    chunk_bytes: int = MAX_CONTENT_RANGE_BYTES,
    max_bytes: int = MAX_CONTENT_EXPORT_BYTES,
) -> str:
    """Perform a bounded UTF-8 export and reject malformed UTF-8.

    Incremental decoding is important here: a UTF-8 codepoint may straddle
    two bounded ranges.  It also ensures malformed legacy bytes become a
    request-scoped error instead of a replacement-character corruption.

    This helper is intentionally an *export* operation. It joins only after
    enforcing ``max_bytes``; callers that need arbitrarily large content must
    consume :func:`iter_content_ranges` instead of using this convenience API.
    """

    import codecs

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ContentExportLimitError("content export max_bytes must be a positive integer")

    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    parts: list[str] = []
    total_bytes = 0
    try:
        async for chunk in iter_content_ranges(reader, ref, chunk_bytes=chunk_bytes):
            total_bytes += len(chunk.data)
            if total_bytes > max_bytes:
                raise ContentExportLimitError(
                    f"content export exceeds {max_bytes} bytes"
                )
            parts.append(decoder.decode(chunk.data, final=False))
        parts.append(decoder.decode(b"", final=True))
    except UnicodeDecodeError as exc:
        raise ContentEncodingError(
            "legacy transcript content is not valid UTF-8"
        ) from exc
    return "".join(parts)


__all__ = [
    "ContentEncodingError",
    "ContentMetadataPendingError",
    "ContentNotFoundError",
    "ContentRange",
    "ContentRangeError",
    "ContentReadError",
    "ContentReader",
    "ContentExportLimitError",
    "MAX_DISPLAY_CONTENT_BYTES",
    "MAX_CONTENT_EXPORT_BYTES",
    "ContentSource",
    "LegacyContentRef",
    "MAX_CONTENT_RANGE_BYTES",
    "iter_content_ranges",
    "read_utf8_text",
    "validate_content_range",
]
