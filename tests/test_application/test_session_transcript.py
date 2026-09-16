"""Tests for the storage- and transport-independent transcript seam."""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.application.session_transcript import (
    SessionPreviewQuery,
    SessionTranscriptApplication,
)


def session(**overrides: Any) -> SimpleNamespace:
    values = {
        "session_key": "agent:main:webchat:default",
        "session_id": "session-default",
        "display_name": "Default chat",
        "derived_title": None,
        "updated_at": 2000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class SessionPort:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.get_calls: list[str] = []
        self.list_limits: list[int] = []

    async def get_session(self, key: str) -> Any | None:
        self.get_calls.append(key)
        return next((row for row in self.rows if row.session_key == key), None)

    async def list_sessions(self, *, limit: int) -> list[Any]:
        self.list_limits.append(limit)
        return self.rows[:limit]


class PreviewPort:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.calls: list[tuple[list[str], int]] = []

    async def list_last_transcript_content_batch(
        self,
        session_ids: list[str],
        *,
        max_chars: int,
    ) -> dict[str, str]:
        self.calls.append((list(session_ids), max_chars))
        return self.values


class FixedClock:
    def now_ms(self) -> int:
        return 1234


def app(
    rows: list[Any],
    values: dict[str, str] | None = None,
) -> tuple[SessionTranscriptApplication, SessionPort, PreviewPort]:
    sessions = SessionPort(rows)
    preview = PreviewPort(values)
    return (
        SessionTranscriptApplication(
            sessions=sessions,
            preview_content=preview,
            clock=FixedClock(),
        ),
        sessions,
        preview,
    )


@pytest.mark.asyncio
async def test_preview_preserves_selection_order_and_uses_one_bounded_projection() -> None:
    first = session(
        session_key="agent:main:webchat:first",
        session_id="first-id",
        display_name=None,
        derived_title="Derived title",
    )
    second = session(
        session_key="agent:main:webchat:second",
        session_id="second-id",
        display_name="Second",
    )
    transcript, sessions, preview = app(
        [first, second],
        {"first-id": "latest", "second-id": "other"},
    )

    result = await transcript.preview(
        SessionPreviewQuery(
            keys=(second.session_key, first.session_key, "missing"),
            limit=50,
        )
    )

    assert [item.key for item in result.previews] == [second.session_key, first.session_key]
    assert [item.title for item in result.previews] == ["Second", "Derived title"]
    assert [item.last_message for item in result.previews] == ["other", "latest"]
    assert result.ts == 1234
    assert sessions.get_calls == [second.session_key, first.session_key, "missing"]
    assert preview.calls == [(["second-id", "first-id"], 120)]


@pytest.mark.asyncio
async def test_preview_uses_bounded_list_port_and_does_not_swallow_errors() -> None:
    row = session(display_name=None, derived_title=None, session_id="fallback-id")
    transcript, sessions, preview = app([row])

    result = await transcript.preview(SessionPreviewQuery(limit=1))

    assert result.previews[0].title == "fallback"
    assert sessions.list_limits == [1]
    assert preview.calls == [(["fallback-id"], 120)]

    class BrokenPreview(PreviewPort):
        async def list_last_transcript_content_batch(
            self,
            session_ids: Sequence[str],
            *,
            max_chars: int,
        ) -> dict[str, str]:
            raise RuntimeError("projection failed")

    broken = SessionTranscriptApplication(
        sessions=sessions,
        preview_content=BrokenPreview(),
        clock=FixedClock(),
    )
    with pytest.raises(RuntimeError, match="projection failed"):
        await broken.preview(SessionPreviewQuery(limit=1))


@pytest.mark.asyncio
async def test_preview_preserves_duplicate_and_empty_session_ids_for_storage_port() -> None:
    rows = [
        session(session_id="same-id"),
        session(
            session_key="agent:main:webchat:empty",
            session_id="",
            display_name=None,
            derived_title=None,
        ),
        session(session_key="agent:main:webchat:duplicate", session_id="same-id"),
    ]
    transcript, _, preview = app(rows)

    result = await transcript.preview(SessionPreviewQuery(limit=3))

    assert len(result.previews) == 3
    assert preview.calls == [(["same-id", "", "same-id"], 120)]
