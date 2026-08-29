"""Tests for the storage-independent SessionDirectory application seam."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.application.session_directory import (
    SessionDirectory,
    SessionSearchProjection,
)


class Storage:
    def __init__(self, exact: Any | None = None, rows: list[Any] | None = None) -> None:
        self.exact = exact
        self.rows = rows or []
        self.get_calls: list[str] = []
        self.list_limits: list[int] = []

    async def get_session(self, key: str) -> Any | None:
        self.get_calls.append(key)
        return self.exact if self.exact is not None and key == self.exact.session_key else None

    async def list_sessions(self, *, limit: int = 100) -> list[Any]:
        self.list_limits.append(limit)
        return self.rows


def session(**overrides: Any) -> SimpleNamespace:
    values = {
        "session_key": "agent:main:webchat:default",
        "session_id": "session-default",
        "status": "idle",
        "agent_id": "main",
        "model": None,
        "workspace_id": None,
        "created_at": 1000,
        "updated_at": 2000,
        "display_name": "Default chat",
        "derived_title": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_exact_lookup_wins_before_legacy_scan() -> None:
    row = session()
    storage = Storage(exact=row, rows=[session(session_key="agent:other:row")])

    result = await SessionDirectory(storage).resolve("webchat:default")

    assert result.key == row.session_key
    assert result.session_id == row.session_id
    assert result.workspace_id is None
    assert storage.get_calls == ["agent:main:webchat:default"]
    assert storage.list_limits == []


@pytest.mark.asyncio
async def test_fallback_scans_at_the_historical_bound_and_maps_domain_fields() -> None:
    row = session(
        session_key="agent:main:webchat:project",
        session_id="project-id",
        status="running",
        model="openai/gpt-5",
        workspace_id="workspace-1",
    )
    storage = Storage(rows=[row])

    result = await SessionDirectory(storage).resolve("project-id")

    assert result == result.__class__(
        key=row.session_key,
        session_id=row.session_id,
        status="running",
        agent_id="main",
        model="openai/gpt-5",
        workspace_id="workspace-1",
        created_at=1000,
        updated_at=2000,
    )
    assert storage.list_limits == [500]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["display_name", "derived_title"])
async def test_fallback_matches_legacy_title_fields(field: str) -> None:
    row = session(display_name=None, derived_title=None)
    setattr(row, field, "human-friendly-title")
    storage = Storage(rows=[row])

    result = await SessionDirectory(storage).resolve("human-friendly-title")

    assert result.key == row.session_key
    assert result.session_id == row.session_id


@pytest.mark.asyncio
async def test_fallback_does_not_scan_beyond_the_legacy_bound() -> None:
    rows = [
        session(
            session_key=f"agent:main:legacy-{index}",
            session_id=f"legacy-{index}",
        )
        for index in range(500)
    ]
    rows.append(session(session_key="agent:main:outside-bound", session_id="outside"))

    class BoundedStorage(Storage):
        async def list_sessions(self, *, limit: int = 100) -> list[Any]:
            self.list_limits.append(limit)
            return self.rows[:limit]

    storage = BoundedStorage(rows=rows)

    with pytest.raises(KeyError, match="Session not found: outside"):
        await SessionDirectory(storage).resolve("outside")

    assert storage.list_limits == [500]


@pytest.mark.asyncio
async def test_ambiguous_prefix_and_missing_reference_keep_legacy_errors() -> None:
    rows = [
        session(session_key="agent:default:abc123", session_id="abc123"),
        session(session_key="agent:bench:abc999", session_id="abc999"),
    ]
    storage = Storage(rows=rows)

    with pytest.raises(ValueError, match="Ambiguous session id 'abc'"):
        await SessionDirectory(storage).resolve("abc")

    with pytest.raises(KeyError, match="Session not found: missing"):
        await SessionDirectory(storage).resolve("missing")


class SearchStorage(Storage):
    def __init__(self, *, titles=None, rows=None, like_rows=None):
        super().__init__(rows=titles or [])
        self.rows = titles or []
        self.rows_result = rows or []
        self.like_rows = like_rows or []
        self.search_calls: list[tuple[str, int]] = []
        self.like_calls: list[tuple[str, int]] = []

    async def get_session(self, key: str):
        for row in self.rows:
            if row.session_key == key:
                return row
        return None

    async def search_sessions_by_title(self, query: str, limit: int = 20):
        return [row for row in self.rows if query.lower() in row.display_name.lower()][:limit]

    async def search_transcript(self, query: str, session_id=None, limit: int = 20):
        self.search_calls.append((query, limit))
        return self.rows_result[:limit]

    async def search_transcript_like(self, query: str, session_id=None, limit: int = 20):
        self.like_calls.append((query, limit))
        return self.like_rows[:limit]


def _search_projection(row: Any, transcript_title: str) -> SessionSearchProjection:
    return SessionSearchProjection(
        title=row.display_name or transcript_title,
        effective_agent_id="main",
        surface="webchat",
        updated_at=row.updated_at,
    )


@pytest.mark.asyncio
async def test_search_normalizes_limit_and_keeps_wire_independent_result() -> None:
    row = session(display_name="Deploy planning")
    storage = SearchStorage(titles=[row])

    result = await SessionDirectory(storage).search(
        "  deploy  ",
        "999",
        now_ms=123,
        project=_search_projection,
    )

    assert result.query == "deploy"
    assert result.ts == 123
    assert result.sessions[0].projection.title == "Deploy planning"
    assert storage.search_calls == [("deploy", 50)]


@pytest.mark.asyncio
async def test_search_routes_non_ascii_to_like_and_deduplicates_title_hits() -> None:
    row = session(display_name="部署讨论")
    storage = SearchStorage(
        titles=[row],
        like_rows=[
            {"session_key": row.session_key, "role": "user", "snippet": "部署", "created_at": 1},
            {
                "session_key": row.session_key,
                "role": "assistant",
                "snippet": "部署",
                "created_at": 2,
            },
        ],
    )

    result = await SessionDirectory(storage).search(
        "部署",
        20,
        now_ms=123,
        project=_search_projection,
    )

    assert storage.like_calls == [("部署", 20)]
    assert storage.search_calls == []
    assert len(result.sessions) == 1
    assert result.messages == ()


@pytest.mark.asyncio
async def test_search_transcript_failure_degrades_to_title_results() -> None:
    row = session(display_name="Deploy planning")

    class BrokenStorage(SearchStorage):
        async def search_transcript(self, query, session_id=None, limit=20):
            raise RuntimeError("index unavailable")

    result = await SessionDirectory(BrokenStorage(titles=[row])).search(
        "deploy",
        20,
        now_ms=123,
        project=_search_projection,
    )

    assert [hit.key for hit in result.sessions] == [row.session_key]
    assert result.messages == ()


@pytest.mark.asyncio
async def test_search_title_failure_keeps_legacy_propagation() -> None:
    class BrokenTitleStorage(SearchStorage):
        async def search_sessions_by_title(self, query, limit=20):
            raise RuntimeError("title index unavailable")

    with pytest.raises(RuntimeError, match="title index unavailable"):
        await SessionDirectory(BrokenTitleStorage()).search(
            "deploy",
            20,
            now_ms=123,
            project=_search_projection,
        )


@pytest.mark.asyncio
async def test_search_uses_bounded_title_fallback_for_legacy_storage() -> None:
    row = session(display_name="Deploy planning")

    class LegacyStorage(Storage):
        async def search_transcript(self, query, session_id=None, limit=20):
            return []

    storage = LegacyStorage(rows=[row])
    result = await SessionDirectory(storage).search(
        "deploy",
        20,
        now_ms=123,
        project=_search_projection,
    )

    assert [hit.key for hit in result.sessions] == [row.session_key]
    assert storage.list_limits == [200]


@pytest.mark.asyncio
async def test_search_enriches_content_hit_without_repeating_title_hit() -> None:
    row = session(display_name="Grocery list")
    storage = SearchStorage(
        titles=[row],
        rows=[
            {
                "session_key": row.session_key,
                "role": "user",
                "snippet": ">>>milk<<<",
                "created_at": 10,
            }
        ],
    )

    result = await SessionDirectory(storage).search(
        "milk",
        20,
        now_ms=123,
        project=_search_projection,
    )

    assert result.sessions == ()
    assert len(result.messages) == 1
    assert result.messages[0].title == "Grocery list"
