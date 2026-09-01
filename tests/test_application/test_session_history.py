"""Tests for the storage- and transport-independent history seam."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.application.session_history import (
    HistoryPage,
    SessionHistoryApplication,
    SessionHistoryQuery,
    paginate_transcript,
)
from opensquilla.history_cursor import HistoryCursorInvalidatedError


def entry(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=index,
        message_id=f"m{index}",
        created_at=index,
        role="user",
        content=f"message {index}",
    )


class ActivePort:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.calls: list[str] = []

    async def read_active_transcript(self, session_key: str) -> list[Any]:
        self.calls.append(session_key)
        return self.rows


class CanonicalPort:
    def __init__(self, page: HistoryPage | None = None) -> None:
        self.page = page
        self.calls: list[dict[str, Any]] = []

    async def read_canonical_page(
        self,
        session_key: str,
        *,
        limit: int,
        before: tuple[int, int] | None,
        after: tuple[int, int] | None,
    ) -> HistoryPage | None:
        self.calls.append(
            {
                "session_key": session_key,
                "limit": limit,
                "before": before,
                "after": after,
            }
        )
        return self.page


@pytest.mark.asyncio
async def test_canonical_page_is_preferred_and_metadata_is_normalized() -> None:
    active = ActivePort([entry(1)])
    canonical = CanonicalPort(
        HistoryPage(
            entries=(entry(2),),
            has_more=True,
            canonical_available=False,
            canonical_complete=True,
        )
    )
    app = SessionHistoryApplication(active=active, canonical=canonical)

    result = await app.read_page(
        SessionHistoryQuery(
            session_key="agent:main:webchat:history",
            limit=2,
            before=(3, 3),
            after=(1, 1),
        )
    )

    assert canonical.page is not None
    assert result.entries[0] is canonical.page.entries[0]
    assert result.has_more is True
    assert result.canonical_available is True
    assert result.canonical_complete is True
    assert active.calls == []
    assert canonical.calls == [
        {
            "session_key": "agent:main:webchat:history",
            "limit": 2,
            "before": (3, 3),
            "after": None,
        }
    ]


@pytest.mark.asyncio
async def test_unavailable_canonical_reader_falls_back_with_before_precedence() -> None:
    active = ActivePort([entry(index) for index in range(1, 6)])
    canonical = CanonicalPort(None)
    app = SessionHistoryApplication(active=active, canonical=canonical)

    result = await app.read_page(
        SessionHistoryQuery(
            session_key="agent:main:webchat:history",
            limit=2,
            before=(4, 4),
            after=(1, 1),
        )
    )

    assert [getattr(row, "id") for row in result.entries] == [2, 3]
    assert result.has_more is True
    assert result.canonical_available is False
    assert result.canonical_complete is False
    assert active.calls == ["agent:main:webchat:history"]


@pytest.mark.asyncio
async def test_include_canonical_false_does_not_call_canonical_port() -> None:
    active = ActivePort([entry(index) for index in range(1, 4)])
    canonical = CanonicalPort(
        HistoryPage(
            entries=(entry(9),),
            has_more=False,
            canonical_available=True,
            canonical_complete=True,
        )
    )
    app = SessionHistoryApplication(active=active, canonical=canonical)

    result = await app.read_page(
        SessionHistoryQuery(
            session_key="agent:main:webchat:history",
            limit=2,
            include_canonical=False,
        )
    )

    assert [getattr(row, "id") for row in result.entries] == [2, 3]
    assert canonical.calls == []
    assert active.calls == ["agent:main:webchat:history"]


@pytest.mark.asyncio
async def test_reader_failures_are_not_silently_swallowed() -> None:
    active = ActivePort([entry(1)])

    class BrokenCanonical(CanonicalPort):
        async def read_canonical_page(self, *args: Any, **kwargs: Any) -> HistoryPage | None:
            raise RuntimeError("retryable storage failure")

    app = SessionHistoryApplication(active=active, canonical=BrokenCanonical())
    with pytest.raises(RuntimeError, match="retryable storage failure"):
        await app.read_page(
            SessionHistoryQuery(
                session_key="agent:main:webchat:history",
                limit=1,
            )
        )
    assert active.calls == []


def test_paginate_transcript_preserves_latest_window_and_valid_cursors() -> None:
    rows = [entry(index) for index in range(1, 6)]

    latest, latest_more = paginate_transcript(rows, limit=2)
    forward, forward_more = paginate_transcript(rows, limit=2, after=(2, 2))
    backward, backward_more = paginate_transcript(
        rows,
        limit=2,
        before=(4, 4),
        after=(99, 99),
    )

    assert [getattr(row, "id") for row in latest] == [4, 5]
    assert latest_more is True
    assert [getattr(row, "id") for row in forward] == [3, 4]
    assert forward_more is True
    assert [getattr(row, "id") for row in backward] == [2, 3]
    assert backward_more is True


@pytest.mark.parametrize("direction", ["before", "after"])
def test_paginate_transcript_rejects_missing_cursor(direction: str) -> None:
    kwargs = {direction: (99, 99)}

    with pytest.raises(HistoryCursorInvalidatedError):
        paginate_transcript([entry(1)], limit=2, **kwargs)
    with pytest.raises(HistoryCursorInvalidatedError):
        paginate_transcript([], limit=2, **kwargs)
