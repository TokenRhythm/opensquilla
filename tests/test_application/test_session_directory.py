"""Tests for the storage-independent SessionDirectory application seam."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.application.session_directory import SessionDirectory


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
