"""Application-owned session history page reads.

The Gateway currently owns the v4 request parsing and the final chat-message
projection.  This module owns the storage-independent part that is safe to
extract first: choosing a canonical page when it is available and applying
the same keyset pagination policy to an active transcript fallback.

Adapters translate unexpected concrete storage failures into ``None`` for an
unavailable canonical reader, while preserving retryable and cursor-domain
failures, and translate legacy wire cursors into :class:`HistoryCursor` values.
No RPC, WebSocket, Gateway, or persistence type is allowed to cross this boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from opensquilla.session.history_cursor import (
    HistoryCursor,
    HistoryCursorInvalidatedError,
)


@dataclass(frozen=True, slots=True)
class SessionHistoryQuery:
    """Normalized input for one history page read.

    ``before`` takes precedence over ``after`` when both are present.  The
    Gateway adapter rejects malformed wire values before constructing this
    value; the application receives a positive limit and parsed cursors only.
    """

    session_key: str
    limit: int
    before: HistoryCursor | None = None
    after: HistoryCursor | None = None
    include_canonical: bool = True


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """Transport-neutral result of a bounded history read."""

    entries: tuple[object, ...]
    has_more: bool
    canonical_available: bool
    canonical_complete: bool


class CanonicalHistoryReader(Protocol):
    """Port for the preferred persisted/keyset history projection.

    Returning ``None`` means that the canonical projection is unavailable and
    the application should use the active transcript reader.  Implementations
    must still raise retryable storage failures and cursor invalidations instead
    of converting them to ``None`` so the Gateway can retain explicit errors.
    """

    async def read_canonical_page(
        self,
        session_key: str,
        *,
        limit: int,
        before: HistoryCursor | None,
        after: HistoryCursor | None,
    ) -> HistoryPage | None: ...


class ActiveHistoryReader(Protocol):
    """Port for the legacy active transcript fallback."""

    async def read_active_transcript(self, session_key: str) -> Sequence[object]: ...


class SessionHistoryApplication:
    """Read one history page through narrow, replaceable Ports."""

    def __init__(
        self,
        *,
        active: ActiveHistoryReader,
        canonical: CanonicalHistoryReader | None = None,
    ) -> None:
        self._active = active
        self._canonical = canonical

    async def read_page(self, query: SessionHistoryQuery) -> HistoryPage:
        """Prefer canonical storage, then apply the legacy fallback policy."""

        effective_after = None if query.before is not None else query.after
        if query.include_canonical and self._canonical is not None:
            # The Port deliberately decides which implementation failures are
            # recoverable.  Any exception that reaches here (for example a
            # retryable storage-busy error) must remain visible to the adapter.
            canonical_page = await self._canonical.read_canonical_page(
                query.session_key,
                limit=query.limit,
                before=query.before,
                after=effective_after,
            )
            if canonical_page is not None:
                return HistoryPage(
                    entries=tuple(canonical_page.entries),
                    has_more=bool(canonical_page.has_more),
                    canonical_available=True,
                    canonical_complete=bool(canonical_page.canonical_complete),
                )

        transcript = tuple(
            await self._active.read_active_transcript(query.session_key)
        )
        entries, has_more = paginate_transcript(
            transcript,
            limit=query.limit,
            before=query.before,
            after=effective_after,
        )
        return HistoryPage(
            entries=entries,
            has_more=has_more,
            canonical_available=False,
            canonical_complete=False,
        )


def cursor_for_entry(entry: object) -> HistoryCursor | None:
    """Return the stable integer cursor used by the history Port."""

    created_at = getattr(entry, "created_at", None)
    stable_id = getattr(entry, "id", None) or getattr(entry, "message_id", None)
    if created_at in {None, ""} or stable_id in {None, ""}:
        return None
    try:
        return int(cast(Any, created_at)), int(cast(Any, stable_id))
    except (TypeError, ValueError):
        return None


def paginate_transcript(
    entries: Sequence[object],
    *,
    limit: int,
    before: HistoryCursor | None = None,
    after: HistoryCursor | None = None,
) -> tuple[tuple[object, ...], bool]:
    """Apply the current active-transcript keyset policy.

    Only an absent cursor is treated as an unpositioned read. When both
    cursors are supplied, ``before`` wins and ``after`` is ignored. A parsed
    cursor that does not identify an entry raises a typed invalidation error
    instead of silently returning the latest window.
    """

    rows = tuple(entries)
    if before is not None:
        before_index = _cursor_index(rows, before)
        if before_index is None:
            raise HistoryCursorInvalidatedError(
                "history cursor no longer anchors this session"
            )
        start = max(0, before_index - limit)
        return rows[start:before_index], start > 0

    if after is not None:
        after_index = _cursor_index(rows, after)
        if after_index is None:
            raise HistoryCursorInvalidatedError(
                "history cursor no longer anchors this session"
            )
        start = min(len(rows), after_index + 1)
        end = min(len(rows), start + limit)
        return rows[start:end], end < len(rows)

    if not rows:
        return (), False
    if len(rows) <= limit:
        return rows, False
    return rows[-limit:], True


def _cursor_index(entries: Sequence[object], cursor: HistoryCursor | None) -> int | None:
    if cursor is None:
        return None
    for index, entry in enumerate(entries):
        if cursor_for_entry(entry) == cursor:
            return index
    return None


__all__ = [
    "ActiveHistoryReader",
    "CanonicalHistoryReader",
    "HistoryCursor",
    "HistoryPage",
    "SessionHistoryApplication",
    "SessionHistoryQuery",
    "cursor_for_entry",
    "paginate_transcript",
]
