"""Gateway adapter for the application-owned session history read seam.

The v4 Gateway has historically combined three concerns in ``rpc_chat``:
request cursor parsing, concrete session-manager/storage calls, and the
transport-neutral choice between a canonical transcript and the active
transcript.  This adapter owns the concrete side of that boundary.  The
application module only receives normalized cursors and narrow reader Ports;
the existing handler remains responsible for the final chat-message
projection and response envelope.

Keeping the adapter separate is intentional.  It lets the WebSocket handler
and the bootstrap composition use exactly one history implementation while
leaving the v4 wire shape and valid-cursor ordering semantics untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from opensquilla.application.session_history import (
    CanonicalHistoryReadError,
    HistoryPage,
    SessionHistoryApplication,
    cursor_for_entry,
    paginate_transcript,
)
from opensquilla.chat.flattened_tool_markers import (
    has_flattened_used_tool_line,
    is_flattened_tool_result_dump,
)
from opensquilla.history_cursor import (
    HistoryCursor,
    HistoryCursorInvalidatedError,
    HistoryCursorInvalidError,
)
from opensquilla.session.storage import StorageBusyError

log = structlog.get_logger(__name__)

_MAX_HISTORY_CURSOR_INTEGER = (1 << 63) - 1
_MAX_HISTORY_CURSOR_INTEGER_TEXT = str(_MAX_HISTORY_CURSOR_INTEGER)


def parse_history_cursor(value: object) -> HistoryCursor | None:
    """Parse the legacy ``created_at|entry_id`` cursor.

    ``None`` represents an absent cursor. Empty, malformed, and values outside
    SQLite's non-negative integer range are supplied-but-invalid inputs and raise a
    typed error instead of being confused with a latest-window read.
    """

    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.count("|") != 1:
        raise HistoryCursorInvalidError(
            "history cursor must use the created_at|id integer format"
        )
    created_at, stable_id = raw.split("|", 1)
    if not all(
        component.isascii() and component.isdecimal()
        for component in (created_at, stable_id)
    ):
        raise HistoryCursorInvalidError(
            "history cursor must use the created_at|id integer format"
        )
    normalized_components = tuple(
        component.lstrip("0") or "0" for component in (created_at, stable_id)
    )
    if any(
        len(component) > len(_MAX_HISTORY_CURSOR_INTEGER_TEXT)
        or (
            len(component) == len(_MAX_HISTORY_CURSOR_INTEGER_TEXT)
            and component > _MAX_HISTORY_CURSOR_INTEGER_TEXT
        )
        for component in normalized_components
    ):
        raise HistoryCursorInvalidError("history cursor integers are out of range")
    return int(normalized_components[0]), int(normalized_components[1])


def canonical_page_parts(page: object) -> tuple[list[object], bool, bool]:
    """Normalize the concrete manager's legacy page return shapes.

    Older managers returned ``(entries, has_more)`` while newer ones return a
    small object carrying ``canonical_complete``.  The shape belongs here at
    the concrete boundary; neither the application service nor the handler
    needs to branch on it.
    """

    if isinstance(page, dict):
        entries = page.get("entries")
        has_more = page.get("has_more", False)
        canonical_complete = page.get("canonical_complete", True)
    elif isinstance(page, tuple):
        entries = page[0] if page else None
        has_more = page[1] if len(page) > 1 else False
        canonical_complete = page[2] if len(page) > 2 else True
    else:
        entries = getattr(page, "entries", None)
        has_more = getattr(page, "has_more", False)
        canonical_complete = getattr(page, "canonical_complete", True)
    if entries is None:
        raise TypeError("canonical transcript page is missing entries")
    return list(entries), bool(has_more), bool(canonical_complete)


class SessionHistoryStorageAdapter:
    """Expose only the two history reader Ports required by the application.

    ``manager`` is deliberately accepted as an opaque object.  The concrete
    Gateway session manager has changed shape several times, and retaining
    duck-typed capability checks here preserves compatibility with old test
    doubles and packaged clients without leaking those checks into the
    application module.
    """

    def __init__(self, manager: object) -> None:
        self._manager = manager

    async def read_canonical_page(
        self,
        session_key: str,
        *,
        limit: int,
        before: HistoryCursor | None,
        after: HistoryCursor | None,
    ) -> HistoryPage | None:
        """Read canonical history, returning ``None`` only when unavailable.

        Unexpected canonical failures are wrapped so the application can try
        active fallback without later misreporting a canonical-only anchor as
        stale. Busy and cursor-domain failures are intentionally preserved so
        the dispatcher can return their explicit error envelopes.
        """

        page_getter = getattr(self._manager, "get_canonical_transcript_page", None)
        if callable(page_getter):
            try:
                page = await page_getter(
                    session_key,
                    limit=limit,
                    before=before,
                    after=after,
                )
                entries, has_more, canonical_complete = canonical_page_parts(page)
                return HistoryPage(
                    entries=tuple(entries),
                    has_more=has_more,
                    canonical_available=True,
                    canonical_complete=canonical_complete,
                )
            except (
                StorageBusyError,
                HistoryCursorInvalidError,
                HistoryCursorInvalidatedError,
            ):
                raise
            except Exception as exc:  # noqa: BLE001 - preserve active fallback
                raise CanonicalHistoryReadError(
                    "canonical history projection failed"
                ) from exc

        getter = getattr(self._manager, "get_canonical_transcript", None)
        if callable(getter):
            try:
                transcript = tuple(await getter(session_key))
                page_entries, has_more = paginate_transcript(
                    transcript,
                    limit=limit,
                    before=before,
                    after=after,
                )
                return HistoryPage(
                    entries=page_entries,
                    has_more=has_more,
                    canonical_available=True,
                    canonical_complete=True,
                )
            except (
                StorageBusyError,
                HistoryCursorInvalidError,
                HistoryCursorInvalidatedError,
            ):
                raise
            except Exception as exc:  # noqa: BLE001 - preserve active fallback
                raise CanonicalHistoryReadError(
                    "canonical history projection failed"
                ) from exc
        return None

    async def read_active_transcript(self, session_key: str) -> Sequence[object]:
        """Read the legacy active transcript, preserving missing capability semantics."""

        getter = getattr(self._manager, "get_transcript", None)
        if not callable(getter):
            return ()
        transcript = await getter(session_key)
        return tuple(transcript or ())

    def application(self) -> SessionHistoryApplication:
        """Compose the transport-neutral history application for this manager."""

        return SessionHistoryApplication(active=self, canonical=self)

    async def load_legacy_tool_projection_context(
        self,
        session_key: str,
        entries: list[object],
        *,
        canonical_available: bool,
    ) -> tuple[object | None, object | None]:
        """Load one adjacent canonical row at either page edge when required.

        This is a presentation compatibility read, not part of pagination.
        It remains in the Gateway adapter because the marker format is a
        legacy wire projection concern.  Errors have the same behavior as the
        former handler helper: busy is retryable; other failures leave the
        selected page intact and are logged.
        """

        if not entries or not canonical_available:
            return None, None
        page_getter = getattr(
            self._manager,
            "get_canonical_transcript_page",
            None,
        )
        if not callable(page_getter):
            return None, None

        previous_entry = None
        next_entry = None
        oldest_cursor = cursor_for_entry(entries[0])
        newest_cursor = cursor_for_entry(entries[-1])

        if _needs_legacy_tool_lookbehind(entries[0]) and oldest_cursor is not None:
            try:
                page = await page_getter(
                    session_key,
                    limit=1,
                    before=oldest_cursor,
                    after=None,
                )
                candidates, _has_more, _complete = canonical_page_parts(page)
            except StorageBusyError:
                raise
            except Exception as exc:  # noqa: BLE001 - optional projection read
                log.warning(
                    "chat_history_legacy_projection_context_unavailable",
                    edge="before",
                    error_type=type(exc).__name__,
                )
                return None, None
            if candidates:
                candidate = candidates[-1]
                candidate_cursor = cursor_for_entry(candidate)
                if candidate_cursor is not None and candidate_cursor < oldest_cursor:
                    previous_entry = candidate

        if _needs_legacy_tool_lookahead(entries[-1]) and newest_cursor is not None:
            try:
                page = await page_getter(
                    session_key,
                    limit=1,
                    before=None,
                    after=newest_cursor,
                )
                candidates, _has_more, _complete = canonical_page_parts(page)
            except StorageBusyError:
                raise
            except Exception as exc:  # noqa: BLE001 - optional projection read
                log.warning(
                    "chat_history_legacy_projection_context_unavailable",
                    edge="after",
                    error_type=type(exc).__name__,
                )
                return None, None
            if candidates:
                candidate = candidates[0]
                candidate_cursor = cursor_for_entry(candidate)
                if candidate_cursor is not None and candidate_cursor > newest_cursor:
                    next_entry = candidate
        return previous_entry, next_entry


def build_session_history_application(manager: object) -> SessionHistoryApplication:
    """Build the history application and keep adapter ownership explicit."""

    return SessionHistoryStorageAdapter(manager).application()


def _needs_legacy_tool_lookbehind(entry: object | None) -> bool:
    if entry is None or getattr(entry, "tool_call_id", None):
        return False
    role = str(getattr(entry, "role", "") or "").lower()
    content = str(getattr(entry, "content", "") or "")
    return role in {"tool", "user"} and is_flattened_tool_result_dump(content)


def _needs_legacy_tool_lookahead(entry: object | None) -> bool:
    if entry is None or getattr(entry, "tool_calls", None):
        return False
    role = str(getattr(entry, "role", "") or "").lower()
    content = str(getattr(entry, "content", "") or "")
    return role == "assistant" and has_flattened_used_tool_line(content)


__all__ = [
    "SessionHistoryStorageAdapter",
    "build_session_history_application",
    "canonical_page_parts",
    "parse_history_cursor",
]
