"""Package-neutral value types for bounded legacy content reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ContentSource = Literal["active", "compacted"]
MAX_CONTENT_RANGE_BYTES = 1 * 1024 * 1024
# A semantic/display read is an explicit opt-in export.  It is bounded more
# tightly than the raw range seam because the server must materialize the
# already-projected display text before returning it to the renderer.
MAX_DISPLAY_CONTENT_BYTES = 8 * 1024 * 1024


class ContentReadError(RuntimeError):
    """Base error for a legacy content read."""


class ContentNotFoundError(ContentReadError):
    """The requested legacy transcript entry does not exist."""


class ContentMetadataPendingError(ContentReadError):
    """The legacy row exists but its bounded size metadata is not indexed yet."""


class ContentRangeError(ContentReadError, ValueError):
    """The requested range is invalid or exceeds the per-read cap."""


class ContentEncodingError(ContentReadError):
    """The legacy body is not valid UTF-8 when text decoding is requested."""


@dataclass(frozen=True, slots=True)
class LegacyContentRef:
    """Stable identity and metadata for one legacy transcript body."""

    session_id: str
    message_id: str
    source: ContentSource
    byte_length: int
    # Storage row revision used to reject same-length replacement races.
    revision: str | None = None


@dataclass(frozen=True, slots=True)
class ContentRange:
    """One bounded byte range from a legacy body."""

    ref: LegacyContentRef
    offset: int
    limit: int
    data: bytes

    @property
    def end(self) -> int:
        return self.offset + len(self.data)

    @property
    def eof(self) -> bool:
        return self.end >= self.ref.byte_length


def validate_content_range(offset: int, limit: int) -> tuple[int, int]:
    """Validate a finite non-negative range without silently widening it."""

    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ContentRangeError("content range offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ContentRangeError("content range limit must be a positive integer")
    if limit > MAX_CONTENT_RANGE_BYTES:
        raise ContentRangeError(
            f"content range limit exceeds {MAX_CONTENT_RANGE_BYTES} bytes"
        )
    return offset, limit


__all__ = [
    "ContentEncodingError",
    "ContentMetadataPendingError",
    "ContentNotFoundError",
    "ContentRange",
    "ContentRangeError",
    "ContentReadError",
    "ContentSource",
    "LegacyContentRef",
    "MAX_CONTENT_RANGE_BYTES",
    "MAX_DISPLAY_CONTENT_BYTES",
    "validate_content_range",
]
