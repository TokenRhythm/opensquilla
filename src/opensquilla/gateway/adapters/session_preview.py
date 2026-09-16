"""Gateway adapter for the application-owned session preview use case.

The adapter is the only layer in this slice that knows both the v4 response
shape and the concrete session storage object.  The application service stays
transport-neutral; this facade also gives the decorated storage methods the
precise async Port signatures that structural type checking requires.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from opensquilla.application.session_transcript import (
    Clock,
    PreviewContentReader,
    SessionPreviewQuery,
    SessionPreviewResult,
    SessionRecord,
    SessionRecordReader,
    SessionTranscriptApplication,
)
from opensquilla.session.storage import SessionStorage


class SessionPreviewStorageAdapter:
    """Expose only the storage reads required by ``SessionTranscriptApplication``.

    ``SessionStorage`` decorates reads with a lock wrapper whose static return
    type is ``Awaitable``.  A plain async facade makes the application Ports
    explicit and prevents the concrete storage type from leaking into the
    application layer.  The concrete type is accepted at this boundary;
    protocol-shaped test doubles remain confined to Gateway fixture setup.
    """

    def __init__(self, storage: SessionStorage) -> None:
        self._storage = storage

    async def get_session(self, key: str) -> SessionRecord | None:
        return await self._storage.get_session(key)

    async def list_sessions(self, *, limit: int) -> Sequence[SessionRecord]:
        return await self._storage.list_sessions(limit=limit)

    async def list_last_transcript_content_batch(
        self,
        session_ids: list[str],
        *,
        max_chars: int,
    ) -> Mapping[str, str]:
        return await self._storage.list_last_transcript_content_batch(
            session_ids,
            max_chars=max_chars,
        )


class SystemClock:
    """Request clock used at the Gateway boundary.

    A handler may need the timestamp for an early empty response and then pass
    the same clock into the application service.  Cache the first observation
    so both paths retain the historical single-request timestamp semantics.
    """

    def __init__(self) -> None:
        self._value: int | None = None

    def now_ms(self) -> int:
        if self._value is None:
            self._value = int(time.time() * 1000)
        return self._value


def preview_query_from_v4(params: Any) -> SessionPreviewQuery:
    """Translate the legacy v4 preview params into application vocabulary.

    The extraction intentionally keeps the old truthiness rules: an absent or
    empty ``keys`` value uses the storage list path, while a truthy iterable is
    looked up in the caller's order.  Validation and malformed-value errors
    are left to the same application/storage calls that handled them before.
    """

    keys, limit = preview_params_from_v4(params)
    return preview_query_from_v4_values(keys, limit)


def preview_params_from_v4(params: Any) -> tuple[Any, Any]:
    """Read legacy fields without iterating or validating them.

    The old handler performed these ``.get`` calls before checking whether a
    session manager was available.  Keeping this tiny read separate lets the
    Gateway preserve that observable malformed-params error ordering while
    deferring key iteration until the bounded storage section.
    """

    raw = params or {}
    return raw.get("keys"), raw.get("limit", 50)


def preview_query_from_v4_values(keys: Any, limit: Any) -> SessionPreviewQuery:
    """Finish the legacy-to-domain conversion after availability checks.

    ``limit`` is intentionally an ``Any`` compatibility value.  v4 historically
    passed malformed values directly to SQLite, where the same exception and
    dispatcher mapping are observable; clamping or validating here would be a
    wire-level behaviour change.  Later Contract validation can narrow it.
    """

    return SessionPreviewQuery(
        keys=tuple(keys) if keys else (),
        limit=limit,
    )


def preview_result_to_v4(result: SessionPreviewResult) -> dict[str, Any]:
    """Project the domain read model back to the unchanged v4 wire shape."""

    return {
        "ts": result.ts,
        "previews": [
            {
                "key": item.key,
                "title": item.title,
                "lastMessage": item.last_message,
                "updatedAt": item.updated_at,
            }
            for item in result.previews
        ],
    }


def build_session_preview_application(
    storage: SessionStorage,
    *,
    clock: Clock | None = None,
) -> SessionTranscriptApplication:
    """Compose the preview application service from a Gateway storage object."""

    storage_adapter = SessionPreviewStorageAdapter(storage)
    # Keep these assignments as a static conformance check.  If a future
    # facade changes a Port signature, mypy fails here instead of allowing a
    # concrete storage method to leak into the application service.
    sessions: SessionRecordReader = storage_adapter
    preview_content: PreviewContentReader = storage_adapter
    return SessionTranscriptApplication(
        sessions=sessions,
        preview_content=preview_content,
        clock=clock if clock is not None else SystemClock(),
    )


__all__ = [
    "SessionPreviewStorageAdapter",
    "SystemClock",
    "build_session_preview_application",
    "preview_params_from_v4",
    "preview_query_from_v4",
    "preview_query_from_v4_values",
    "preview_result_to_v4",
]
