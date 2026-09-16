"""Application-owned read seams for session previews and transcript history.

This module defines the smallest application vocabulary needed by the first
transcript extraction slice.  It intentionally has no Gateway/RPC, transport,
wire-alias, or concrete persistence imports.  The Gateway remains responsible
for adapting v4 frames to these values until the later extraction slices.

S4a exercises only the preview use case.  The history cursor, reader ports,
and page DTOs now live in the separate :mod:`session_history` module after
the real ``chat.history`` call sites were characterized; keeping them in a
separate module prevents preview and history concerns from growing one
another's interface.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


class SessionRecord(Protocol):
    """Read-only fields needed to build a session preview."""

    @property
    def session_key(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def display_name(self) -> str | None: ...

    @property
    def derived_title(self) -> str | None: ...

    @property
    def updated_at(self) -> int | float | None: ...


class SessionRecordReader(Protocol):
    """Port for selecting session records without exposing a storage type."""

    async def get_session(self, key: str) -> SessionRecord | None: ...

    async def list_sessions(self, *, limit: int) -> Sequence[SessionRecord]: ...


class PreviewContentReader(Protocol):
    """Port for one bounded latest-message projection per session."""

    async def list_last_transcript_content_batch(
        self,
        session_ids: list[str],
        *,
        max_chars: int,
    ) -> Mapping[str, str]: ...


class Clock(Protocol):
    """Clock Port kept separate so application tests are deterministic."""

    def now_ms(self) -> int: ...


@dataclass(frozen=True, slots=True)
class SessionPreviewQuery:
    """Normalized application input for a preview read."""

    keys: tuple[str, ...] = ()
    limit: int = 50


@dataclass(frozen=True, slots=True)
class SessionPreviewItem:
    """A transport-neutral preview projection."""

    key: str
    title: str
    last_message: str
    updated_at: int | float | None


@dataclass(frozen=True, slots=True)
class SessionPreviewResult:
    """The complete preview read model, including its observation time."""

    ts: int
    previews: tuple[SessionPreviewItem, ...]


class SessionTranscriptApplication:
    """Coordinate bounded transcript projections behind narrow Ports."""

    PREVIEW_MAX_CHARS = 120

    def __init__(
        self,
        *,
        sessions: SessionRecordReader,
        preview_content: PreviewContentReader,
        clock: Clock,
    ) -> None:
        self._sessions = sessions
        self._preview_content = preview_content
        self._clock = clock

    async def preview(self, query: SessionPreviewQuery) -> SessionPreviewResult:
        """Read previews without materializing complete transcripts.

        Selection order and title precedence mirror the current Gateway
        implementation.  The projection call is made even for an empty list
        so the application seam preserves the storage Port's observable call
        contract; Port errors propagate for the compatibility adapter to map.
        """

        now_ms = self._clock.now_ms()
        if query.keys:
            selected: list[SessionRecord] = []
            for key in query.keys:
                session = await self._sessions.get_session(key)
                if session is not None:
                    selected.append(session)
        else:
            selected = list(await self._sessions.list_sessions(limit=query.limit))

        session_ids = [str(session.session_id or "") for session in selected]
        last_messages = await self._preview_content.list_last_transcript_content_batch(
            session_ids,
            max_chars=self.PREVIEW_MAX_CHARS,
        )

        previews = tuple(
            SessionPreviewItem(
                key=session.session_key,
                title=self._title(session),
                last_message=self._message(last_messages, session),
                updated_at=session.updated_at,
            )
            for session in selected
        )
        return SessionPreviewResult(ts=now_ms, previews=previews)

    @staticmethod
    def _title(session: SessionRecord) -> str:
        if session.display_name:
            return str(session.display_name)
        if session.derived_title:
            return str(session.derived_title)
        return str(session.session_id or "")[:8]

    @staticmethod
    def _message(messages: Mapping[str, str], session: SessionRecord) -> str:
        value = messages.get(str(session.session_id or ""), "")
        return value if isinstance(value, str) else ""


__all__ = [
    "Clock",
    "PreviewContentReader",
    "SessionPreviewItem",
    "SessionPreviewQuery",
    "SessionPreviewResult",
    "SessionRecord",
    "SessionRecordReader",
    "SessionTranscriptApplication",
]
