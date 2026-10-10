"""Gateway adapter for bounded reads from legacy transcript rows."""

from __future__ import annotations

from opensquilla.application.content_reader import (
    MAX_CONTENT_RANGE_BYTES,
    MAX_DISPLAY_CONTENT_BYTES,
    ContentRange,
    ContentReader,
    ContentSource,
    LegacyContentRef,
)
from opensquilla.session.storage import SessionStorage


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
    ) -> LegacyContentRef:
        return await self._storage.get_legacy_content_ref(
            session_id,
            message_id,
            source=source,
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
        return await self._storage.read_legacy_display_text(ref, max_bytes=max_bytes)


def build_content_reader(
    storage: SessionStorage,
) -> SessionContentReaderStorageAdapter:
    """Compose the read-only legacy content port for a Gateway handler."""

    return SessionContentReaderStorageAdapter(storage)


__all__ = ["SessionContentReaderStorageAdapter", "build_content_reader"]
