"""Tests for the concrete Gateway/session-history application seam."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.application.session_history import (
    CanonicalHistoryReadError,
    SessionHistoryQuery,
)
from opensquilla.gateway.adapters.session_history import (
    SessionHistoryStorageAdapter,
    canonical_page_parts,
    parse_history_cursor,
)
from opensquilla.history_cursor import (
    HistoryCursorInvalidatedError,
    HistoryCursorInvalidError,
)
from opensquilla.session.storage import StorageBusyError


def row(index: int, *, role: str = "user", content: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=index,
        message_id=f"m{index}",
        created_at=index,
        role=role,
        content=content if content is not None else f"message {index}",
    )


def test_parse_history_cursor_distinguishes_absent_and_valid_values() -> None:
    assert parse_history_cursor(None) is None
    assert parse_history_cursor(" 2|7 ") == (2, 7)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not-a-cursor",
        "1|not-an-int",
        "1|2|3",
        "1_0|2",
        "+10|2",
        "١٠|٢",
        "10 |2",
        "10| 2",
        "-1|2",
        "1|-2",
        f"{1 << 63}|1",
        f"1|{1 << 63}",
    ],
)
def test_parse_history_cursor_rejects_invalid_values(value: object) -> None:
    with pytest.raises(HistoryCursorInvalidError):
        parse_history_cursor(value)


def test_canonical_page_parts_accepts_legacy_shapes() -> None:
    first = row(1)
    second = row(2)
    assert canonical_page_parts({"entries": [first], "has_more": 1}) == (
        [first],
        True,
        True,
    )
    assert canonical_page_parts(([first, second], False, False)) == (
        [first, second],
        False,
        False,
    )
    assert canonical_page_parts(
        SimpleNamespace(entries=(second,), has_more=True, canonical_complete=False)
    ) == ([second], True, False)


class CanonicalManager:
    def __init__(self, page: object) -> None:
        self.page = page
        self.calls: list[dict[str, Any]] = []
        self.active_calls: list[str] = []

    async def get_canonical_transcript_page(
        self,
        session_key: str,
        *,
        limit: int,
        before: tuple[int, int] | None = None,
        after: tuple[int, int] | None = None,
    ) -> object:
        self.calls.append(
            {
                "session_key": session_key,
                "limit": limit,
                "before": before,
                "after": after,
            }
        )
        return self.page

    async def get_transcript(self, session_key: str) -> list[SimpleNamespace]:
        self.active_calls.append(session_key)
        return [row(index) for index in range(1, 5)]


@pytest.mark.asyncio
async def test_adapter_composes_application_and_prefers_canonical_page() -> None:
    manager = CanonicalManager(
        SimpleNamespace(
            entries=(row(3),),
            has_more=True,
            canonical_complete=True,
        )
    )
    adapter = SessionHistoryStorageAdapter(manager)

    result = await adapter.application().read_page(
        SessionHistoryQuery(
            session_key="agent:main:webchat:history",
            limit=2,
            before=(4, 4),
        )
    )

    assert [getattr(item, "id") for item in result.entries] == [3]
    assert result.has_more is True
    assert result.canonical_available is True
    assert result.canonical_complete is True
    assert manager.active_calls == []
    assert manager.calls == [
        {
            "session_key": "agent:main:webchat:history",
            "limit": 2,
            "before": (4, 4),
            "after": None,
        }
    ]


@pytest.mark.asyncio
async def test_adapter_falls_back_to_active_transcript_on_canonical_failure() -> None:
    class BrokenManager(CanonicalManager):
        async def get_canonical_transcript_page(self, *args: Any, **kwargs: Any) -> object:
            raise RuntimeError("projection unavailable")

    manager = BrokenManager(None)
    adapter = SessionHistoryStorageAdapter(manager)
    result = await adapter.application().read_page(
        SessionHistoryQuery(
            session_key="agent:main:webchat:history",
            limit=2,
            before=(4, 4),
            after=(1, 1),
        )
    )

    assert [getattr(item, "id") for item in result.entries] == [2, 3]
    assert result.has_more is True
    assert result.canonical_available is False
    assert result.canonical_complete is False
    assert manager.active_calls == ["agent:main:webchat:history"]


@pytest.mark.asyncio
async def test_adapter_does_not_mislabel_canonical_only_cursor_on_projection_failure() -> None:
    failure = RuntimeError("projection unavailable")

    class BrokenManager(CanonicalManager):
        async def get_canonical_transcript_page(self, *args: Any, **kwargs: Any) -> object:
            raise failure

        async def get_transcript(self, session_key: str) -> list[SimpleNamespace]:
            self.active_calls.append(session_key)
            return [row(1)]

    manager = BrokenManager(None)
    adapter = SessionHistoryStorageAdapter(manager)
    with pytest.raises(CanonicalHistoryReadError) as caught:
        await adapter.application().read_page(
            SessionHistoryQuery(
                session_key="agent:main:webchat:history",
                limit=2,
                before=(4, 4),
            )
        )

    assert caught.value.__cause__ is failure
    assert manager.active_calls == ["agent:main:webchat:history"]


@pytest.mark.asyncio
async def test_adapter_preserves_storage_busy_error() -> None:
    class BusyManager(CanonicalManager):
        async def get_canonical_transcript_page(self, *args: Any, **kwargs: Any) -> object:
            raise StorageBusyError(
                "chat.history",
                waited_ms=10,
                retry_after_ms=100,
                stage="read",
                resource="transcript",
            )

    manager = BusyManager(None)
    adapter = SessionHistoryStorageAdapter(manager)
    with pytest.raises(StorageBusyError):
        await adapter.application().read_page(
            SessionHistoryQuery(
                session_key="agent:main:webchat:history",
                limit=1,
            )
        )
    assert manager.active_calls == []


@pytest.mark.asyncio
async def test_adapter_preserves_cursor_invalidation_without_active_fallback() -> None:
    class InvalidatedManager(CanonicalManager):
        async def get_canonical_transcript_page(self, *args: Any, **kwargs: Any) -> object:
            raise HistoryCursorInvalidatedError("anchor missing")

    manager = InvalidatedManager(None)
    adapter = SessionHistoryStorageAdapter(manager)
    with pytest.raises(HistoryCursorInvalidatedError):
        await adapter.application().read_page(
            SessionHistoryQuery(
                session_key="agent:main:webchat:history",
                limit=1,
                before=(2, 2),
            )
        )
    assert manager.active_calls == []


@pytest.mark.asyncio
async def test_adapter_uses_full_canonical_getter_when_page_capability_is_absent() -> None:
    class FullCanonicalManager:
        async def get_canonical_transcript(self, session_key: str) -> list[object]:
            return [row(index) for index in range(1, 6)]

        async def get_transcript(self, session_key: str) -> list[object]:
            raise AssertionError("active fallback should not run")

    adapter = SessionHistoryStorageAdapter(FullCanonicalManager())
    result = await adapter.application().read_page(
        SessionHistoryQuery(
            session_key="agent:main:webchat:history",
            limit=2,
            after=(2, 2),
        )
    )
    assert [getattr(item, "id") for item in result.entries] == [3, 4]
    assert result.has_more is True
    assert result.canonical_available is True
    assert result.canonical_complete is True


@pytest.mark.asyncio
async def test_projection_context_uses_typed_cursors_and_preserves_edges() -> None:
    class ProjectionManager(CanonicalManager):
        async def get_canonical_transcript_page(
            self,
            session_key: str,
            *,
            limit: int,
            before: tuple[int, int] | None = None,
            after: tuple[int, int] | None = None,
        ) -> object:
            self.calls.append(
                {
                    "session_key": session_key,
                    "limit": limit,
                    "before": before,
                    "after": after,
                }
            )
            if before == (2, 2):
                return ([row(1)], False)
            if after == (2, 2):
                return ([row(3)], False)
            return ([row(2, role="tool", content="tool result")], False)

    manager = ProjectionManager(None)
    adapter = SessionHistoryStorageAdapter(manager)
    entries = [
        row(2, role="tool", content="[Tool result (x): tool result]"),
        row(2, role="assistant", content="[Used tool: x]"),
    ]
    previous, following = await adapter.load_legacy_tool_projection_context(
        "agent:main:webchat:history",
        entries,
        canonical_available=True,
    )
    assert previous is not None and getattr(previous, "id") == 1
    assert following is not None and getattr(following, "id") == 3
