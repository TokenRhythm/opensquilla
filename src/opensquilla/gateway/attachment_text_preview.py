"""Bounded UTF-8 text previews for immutable transcript material."""

from __future__ import annotations

import codecs
import os
from pathlib import Path
from typing import TypedDict

MAX_PREVIEW_BYTES = 256 * 1024
MAX_PREVIEW_LINES = 1000
MAX_PREVIEW_LINE_BYTES = 8 * 1024


class AttachmentTextPreview(TypedDict):
    content: str
    truncated: bool
    totalBytes: int


def read_attachment_text_preview(path: Path) -> AttachmentTextPreview:
    """Read a literal prefix, stopping at the first byte, line, or line-width cap.

    The text reader keeps literal newline sequences and the UTF-8 BOM. Each
    read is bounded even for a single enormous line; the retained prefix is
    then limited by UTF-8 bytes without returning an incomplete character.
    """

    retained = bytearray()
    lines = 0
    with path.open("r", encoding="utf-8", newline="") as source:
        total_bytes = os.fstat(source.fileno()).st_size
        while len(retained) < MAX_PREVIEW_BYTES and lines < MAX_PREVIEW_LINES:
            limit = min(MAX_PREVIEW_LINE_BYTES, MAX_PREVIEW_BYTES - len(retained))
            line = source.readline(limit + 1)
            if not line:
                break
            chunk = line.encode("utf-8")
            if len(chunk) > limit:
                prefix = chunk[:limit]
                # Keep a CRLF terminator together when the byte cap bisects it.
                if prefix.endswith(b"\r") and chunk[limit:limit + 1] == b"\n":
                    prefix = prefix[:-1]
                retained.extend(prefix)
                break
            retained.extend(chunk)
            lines += 1
            if not chunk.endswith((b"\n", b"\r")):
                break

    truncated = len(retained) < total_bytes
    # utf-8, rather than utf-8-sig, deliberately retains a literal leading BOM.
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    content = decoder.decode(retained, final=not truncated)
    return {"content": content, "truncated": truncated, "totalBytes": total_bytes}
