"""Gateway adapter for bounded reads from legacy transcript rows."""

from __future__ import annotations

import asyncio
import json

from opensquilla.application.content_reader import (
    MAX_CONTENT_RANGE_BYTES,
    MAX_DISPLAY_CONTENT_BYTES,
    ContentExportLimitError,
    ContentRange,
    ContentReader,
    ContentSource,
    LegacyContentRef,
)
from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage


def _project_display(entry: TranscriptEntry, *, details: bool, max_bytes: int) -> str:
    messages = transcript_entries_to_chat_messages([entry], content_mode="legacy")
    message = messages[0] if messages else {}
    if details:
        projected = {
            key: message[key] for key in (
                "id", "message_id", "role", "reasoning_content", "tool_calls", "turn_context",
            ) if key in message
        }
        text = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    else:
        value = message.get("text")
        text = value if isinstance(value, str) else ""
    if len(text.encode("utf-8")) > max_bytes:
        if details:
            raise ContentExportLimitError(f"display details exceed {max_bytes} bytes")
        raise ContentExportLimitError(f"display content exceeds {max_bytes} bytes")
    return text


class SessionContentReaderStorageAdapter(ContentReader):
    """Keep concrete SQLite ownership outside the application port."""

    def __init__(self, storage: SessionStorage) -> None:
        self._storage = storage

    async def get_ref(
        self,
        session_id: str,
        message_id: str,
        *,
        source: ContentSource | None = None,
        allow_pending: bool = False,
    ) -> LegacyContentRef:
        return await self._storage.get_legacy_content_ref(
            session_id,
            message_id,
            source=source,
            allow_pending=allow_pending,
        )

    async def read_range(
        self,
        ref: LegacyContentRef,
        *,
        offset: int = 0,
        limit: int = MAX_CONTENT_RANGE_BYTES,
    ) -> ContentRange:
        return await self._storage.read_legacy_content_range(
            ref,
            offset=offset,
            limit=limit,
        )

    async def read_display_text(
        self,
        ref: LegacyContentRef,
        *,
        max_bytes: int = MAX_DISPLAY_CONTENT_BYTES,
    ) -> str:
        entry = await self._storage.read_legacy_display_entry(ref, max_bytes=max_bytes)
        return await asyncio.to_thread(_project_display, entry, details=False, max_bytes=max_bytes)

    async def read_display_details(
        self,
        ref: LegacyContentRef,
        *,
        max_bytes: int = MAX_DISPLAY_CONTENT_BYTES,
    ) -> str:
        entry = await self._storage.read_legacy_detail_entry(ref, max_bytes=max_bytes)
        return await asyncio.to_thread(_project_display, entry, details=True, max_bytes=max_bytes)


def build_content_reader(
    storage: SessionStorage,
) -> SessionContentReaderStorageAdapter:
    """Compose the read-only legacy content port for a Gateway handler."""

    return SessionContentReaderStorageAdapter(storage)


__all__ = ["SessionContentReaderStorageAdapter", "build_content_reader"]
