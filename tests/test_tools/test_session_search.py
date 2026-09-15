"""Registered session_search routing, persisted results, and access boundaries."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage
from opensquilla.tool_boundary import ToolCall
from opensquilla.tools.builtin import session_search
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import (
    CallerKind,
    PlanAccess,
    ToolContext,
    ToolError,
    current_tool_context,
)


@pytest.fixture(autouse=True)
def restore_storage(monkeypatch):
    monkeypatch.setattr(session_search, "_storage", None)


def register(storage):
    registry = ToolRegistry()
    session_search.create_session_search_tool(storage, registry=registry)
    registered = registry.get("session_search")
    assert registered is not None
    return registry, registered


@pytest.fixture
def recorded():
    storage = SimpleNamespace(
        search_transcript=AsyncMock(return_value=[]),
        search_transcript_like=AsyncMock(return_value=[]),
    )
    registry, registered = register(storage)
    return storage, registry, registered


@pytest.mark.parametrize(
    "query,route",
    [
        pytest.param("deploy API", "search_transcript", id="ascii"),
        pytest.param("部署", "search_transcript_like", id="chinese"),
        pytest.param("  API 部署  ", "search_transcript_like", id="mixed-original-query"),
        pytest.param("ПРИВЕТ", "search_transcript_like", id="cyrillic"),
        pytest.param("café", "search_transcript_like", id="accented-latin"),
        pytest.param("deploy 🚀", "search_transcript_like", id="emoji"),
    ],
)
async def test_routes_original_query(recorded, query, route):
    storage, _, registered = recorded

    result = json.loads(await registered.handler(query=query))

    getattr(storage, route).assert_awaited_once_with(query=query, session_id=None, limit=20)
    other = "search_transcript_like" if route == "search_transcript" else "search_transcript"
    getattr(storage, other).assert_not_awaited()
    assert result == {"query": query, "results": [], "note": "No matches found."}


@pytest.mark.parametrize("query", ["deploy", "部署"], ids=["fts", "like"])
@pytest.mark.parametrize("limit,expected", [(-10, 1), (0, 1), (1, 1), (7, 7), (50, 50), (99, 50)])
async def test_scoped_limit_is_clamped(recorded, query, limit, expected):
    storage, _, registered = recorded

    await registered.handler(query=query, session_id="session-two", limit=limit)

    selected = storage.search_transcript if query == "deploy" else storage.search_transcript_like
    selected.assert_awaited_once_with(query=query, session_id="session-two", limit=expected)


@pytest.mark.parametrize(
    "query", ["", " \t\n", "\u3000"], ids=["empty", "ascii-space", "cjk-space"]
)
async def test_empty_query_fails_before_storage(recorded, query):
    storage, _, registered = recorded
    with pytest.raises(ToolError, match="Query must not be empty"):
        await registered.handler(query=query)
    storage.search_transcript.assert_not_awaited()
    storage.search_transcript_like.assert_not_awaited()


@pytest.mark.parametrize("query", ["deploy", "部署"], ids=["fts", "like"])
async def test_storage_failure_preserves_error_response(recorded, query):
    storage, _, registered = recorded
    selected = storage.search_transcript if query == "deploy" else storage.search_transcript_like
    selected.side_effect = RuntimeError("internal database detail")

    result = json.loads(await registered.handler(query=query))

    assert result == {"query": query, "results": [], "error": "Search failed"}


def test_registration_retains_access_contract(recorded):
    _, _, registered = recorded
    assert registered.spec.owner_only is True
    assert registered.spec.plan_access is PlanAccess.READ_ONLY
    assert registered.spec.required == ["query"]
    assert set(registered.spec.parameters) == {"query", "session_id", "limit"}


@pytest.fixture
async def persisted(tmp_path):
    db_path = str(tmp_path / "sessions.db")
    # Separate ASCII-only and Chinese-only decoys expose dropped-term/OR regressions.
    contents = [
        ("one", "项目讨论：今天部署 API 接口，测试通过。"),
        ("one", "API only reference"),
        ("one", "只有部署说明"),
        ("one", "Обсудили ПРИВЕТ и CAFÉ"),
        ("two", "部署流程由 API 完成，另一个会话。"),
        ("one", "比例 100% 已完成"),
        ("one", "比例 1000 已完成"),
        ("one", "路径 名_称"),
        ("one", "路径 名X称"),
        ("one", r"路径 c:\报告"),
        ("one", "路径 c:报告"),
    ]
    async with SessionStorage(db_path) as storage:
        for name in ("one", "two"):
            await storage.upsert_session(
                SessionNode(session_key=f"agent:main:webchat:{name}", session_id=name)
            )
        for timestamp, (name, content) in enumerate(contents, start=1):
            await storage.append_transcript_entry(
                TranscriptEntry(
                    session_id=name,
                    session_key=f"agent:main:webchat:{name}",
                    role="assistant",
                    content=content,
                    created_at=timestamp,
                )
            )
    # Register against a reopened database so search cannot depend on live chat state.
    async with SessionStorage(db_path) as storage:
        changes_before = storage.conn.total_changes
        _, registered = register(storage)
        yield registered
        assert storage.conn.total_changes == changes_before


@pytest.mark.parametrize(
    "query,session_id,limit,expected",
    [
        pytest.param("部署", None, 20, {1, 3, 5}, id="chinese-substring"),
        pytest.param("API 部署", None, 20, {1, 5}, id="mixed-and-excludes-decoys"),
        pytest.param("  API 部署  ", "one", 20, {1}, id="scope-one"),
        pytest.param("API 部署", "two", 20, {5}, id="scope-two"),
        pytest.param("API 部署", None, 1, {5}, id="limit-newest"),
        pytest.param("привет", None, 20, {4}, id="cyrillic-case-fold"),
        pytest.param("café", None, 20, {4}, id="accented-case-fold"),
        pytest.param("比例 100%", None, 20, {6}, id="literal-percent"),
        pytest.param("名_称", None, 20, {8}, id="literal-underscore"),
        pytest.param(r"c:\报告", None, 20, {10}, id="literal-backslash"),
        pytest.param("API", None, 20, {1, 2, 5}, id="ascii-fts"),
    ],
)
async def test_persisted_matches_and_schema(persisted, query, session_id, limit, expected):
    result = json.loads(await persisted.handler(query=query, session_id=session_id, limit=limit))

    assert set(result) == {"query", "result_count", "results"}
    assert result["query"] == query
    assert result["result_count"] == len(expected)
    assert {row["created_at"] for row in result["results"]} == expected
    for row in result["results"]:
        assert set(row) == {"session_key", "role", "snippet", "created_at"}
        name = "two" if row["created_at"] == 5 else "one"
        assert row["session_key"] == f"agent:main:webchat:{name}"
        assert row["role"] == "assistant"
        assert ">>>" in row["snippet"] and "<<<" in row["snippet"]


@pytest.mark.parametrize("query", ["absent", "未曾记录"], ids=["fts", "like"])
async def test_persisted_no_hits(persisted, query):
    result = json.loads(await persisted.handler(query=query))
    assert result == {"query": query, "results": [], "note": "No matches found."}


@pytest.mark.parametrize(
    "is_owner,caller_kind,session_key,error_class",
    [
        pytest.param(
            False, CallerKind.AGENT, "agent:main:webchat:one", "OwnerOnly", id="non-owner"
        ),
        pytest.param(
            True,
            CallerKind.CHANNEL,
            "agent:main:slack:group:g1",
            "PolicyDenied",
            id="shared-group",
        ),
        pytest.param(
            True,
            CallerKind.SUBAGENT,
            "subagent:agent:main:parent",
            "PolicyDenied",
            id="owner-subagent",
        ),
    ],
)
async def test_dispatch_denies_before_storage(
    recorded, is_owner, caller_kind, session_key, error_class
):
    storage, registry, _ = recorded
    handler = build_tool_handler(
        registry,
        ToolContext(is_owner=is_owner, caller_kind=caller_kind, session_key=session_key),
    )
    token = current_tool_context.set(None)
    try:
        result = await handler(
            ToolCall(
                tool_use_id="search-denied",
                tool_name="session_search",
                arguments={"query": "API 部署"},
            )
        )
    finally:
        current_tool_context.reset(token)

    assert result.is_error is True
    assert json.loads(result.content)["error_class"] == error_class
    storage.search_transcript.assert_not_awaited()
    storage.search_transcript_like.assert_not_awaited()
