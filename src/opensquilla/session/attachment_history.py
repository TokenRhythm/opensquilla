"""Bounded, read-only display projection of persisted user envelopes.

SQL removes inline material before a page crosses the SQLite/Python boundary.
The canonical envelope and provider replay are deliberately unchanged.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json

from opensquilla.contracts.attachments import (
    MAX_ATTACHMENTS,
    MAX_TOTAL_ATTACHMENT_BYTES,
    attachment_size_limit_for_mime,
)

DISPLAY_TEXT_CHARS = 16 * 1024 + 1
MAX_DISPLAY_ENVELOPE_BYTES = 256 * 1024
MAX_INLINE_ENVELOPE_BYTES = (MAX_TOTAL_ATTACHMENT_BYTES + 2) // 3 * 4 + 8 * 1024 * 1024
INLINE_CHUNK_CHARS = 256 * 1024  # divisible by four for independent base64 decoding
USER_DISPLAY_TEXT_SQL = (
    "CASE WHEN json_type(content, '$.display_text') = 'text' "
    "THEN json_extract(content, '$.display_text') ELSE json_extract(content, '$.text') END"
)


def inline_attachment_digest(data: object, mime: object, budget: int) -> str:
    """SQLite-worker scalar: one admitted attachment, never a whole JSON row."""
    if not isinstance(data, str) or not isinstance(mime, str):
        return '{"error":"invalid inline attachment"}'
    limit = min(max(0, int(budget)), attachment_size_limit_for_mime(mime))
    if not data or len(data) % 4 or len(data) > ((limit + 2) // 3) * 4:
        return '{"error":"attachment exceeds the read limit"}'
    digest = hashlib.sha256()
    size = 0
    try:
        for offset in range(0, len(data), INLINE_CHUNK_CHARS):
            part = data[offset:offset + INLINE_CHUNK_CHARS]
            if offset + len(part) < len(data) and "=" in part:
                raise ValueError("premature padding")
            chunk = base64.b64decode(part, validate=True)
            size += len(chunk)
            if size > limit:
                raise ValueError("decoded limit exceeded")
            digest.update(chunk)
    except (ValueError, binascii.Error):
        return '{"error":"invalid inline attachment"}'
    return json.dumps({"sha256_ref": digest.hexdigest(), "size": size})


def user_display_envelope_sql() -> str:
    """Return NULL for ordinary text; a bounded JSON result for user envelopes."""
    paths = ", ".join(f"'$.attachments[{index}].data'" for index in range(MAX_ATTACHMENTS))
    visible = USER_DISPLAY_TEXT_SQL
    stripped = f"json_remove(content, {paths})"
    # Keep all existing envelope metadata. Only text and attachment bytes have
    # distinct display/read paths; no allowlist silently drops new metadata.
    display = (
        f"json_set({stripped}, '$.text', substr({visible}, 1, {DISPLAY_TEXT_CHARS}), "
        f"'$.display_text', substr({visible}, 1, {DISPLAY_TEXT_CHARS}))"
    )
    return (
        f"""CASE WHEN role = 'user' THEN CASE
        WHEN length(CAST(content AS BLOB)) > {MAX_INLINE_ENVELOPE_BYTES} THEN
          CASE WHEN ltrim(substr(content, 1, 4096)) LIKE '{{%'
            THEN '{{"error":"structured message exceeds the display read limit"}}' END
        ELSE CASE WHEN json_valid(content) THEN CASE
          WHEN json_type(content, '$.text') = 'text'
           AND json_type(content, '$.attachments') = 'array'
          THEN CASE WHEN json_array_length(content, '$.attachments') > {MAX_ATTACHMENTS}
            THEN '{{"error":"attachment count exceeds the supported limit"}}'
            WHEN length(CAST({display} AS BLOB)) > {MAX_DISPLAY_ENVELOPE_BYTES}
            THEN '{{"error":"attachment display metadata exceeds the supported limit"}}'
            ELSE json_object('envelope', json({display}),
                 'text_truncated', length({visible}) > {DISPLAY_TEXT_CHARS},
                 'display_byte_length', length(CAST({visible} AS BLOB)),
                 'local_path_validation_text', CASE
                    WHEN json_type(content, '$.local_path_references') = 'array'
                     AND length({visible}) <= 100000 THEN {visible} END,
                 'inline_ordinals', json((SELECT json_group_array(key)
                    FROM json_each(content, '$.attachments')
                    WHERE CASE WHEN type = 'object' THEN """
        f"""json_type(value, '$.data') END IS NOT NULL)))
            END
          WHEN json_type(content, '$.text') = 'text'
           AND json_type(content, '$.attachments') IS NOT NULL
          THEN '{{"error":"invalid attachment metadata"}}'
          END
          WHEN length(CAST(content AS BLOB)) > 65536
           AND ltrim(substr(content, 1, 4096)) LIKE '{{%'
          THEN '{{"error":"invalid structured message"}}'
          END END END"""
    )
