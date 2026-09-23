"""Result and work bounds for session summaries shared by multiple callers."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.rpc_sessions import _list_task_rows_by_session, _list_transcript_titles
from opensquilla.gateway.subagent_announce import _list_latest_task_rows_for_sessions
from opensquilla.session import storage as storage_module
from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.builtin.session_search import create_session_search_tool
from opensquilla.tools.registry import ToolRegistry


async def seed_tasks(storage, keys, count):
    await storage.conn.executemany(
        """INSERT INTO agent_tasks
        (task_id, session_key, source_kind, queue_mode, status, created_at, updated_at, details)
        VALUES (?, ?, 'webui', 'followup', ?, ?, ?, ?)""",
        [
            (f"{key}-{index:04d}", key, "failed" if index == count - 1 else "succeeded",
             index // 3, index // 3, '{"evidence":"exact-only"}')
            for key in keys for index in range(count)
        ],
    )
    await storage.conn.commit()


@pytest.mark.asyncio
async def test_task_summary_bounds_rows_before_python_and_matches_old_order(tmp_path, monkeypatch):
    keys = [f"agent:main:webchat:bounded-{i}" for i in range(10)]
    async with SessionStorage(str(tmp_path / "sessions.db")) as storage:
        await seed_tasks(storage, keys, 1000)
        async with storage.conn.execute(
            "SELECT task_id, session_key FROM agent_tasks "
            "ORDER BY session_key ASC, created_at DESC, rowid DESC"
        ) as cursor:
            oracle = await cursor.fetchall()
            cursor_class = type(cursor)
        expected = {key: [] for key in keys}
        for row in oracle:
            if len(expected[row["session_key"]]) < 100:
                expected[row["session_key"]].append(row["task_id"])

        fetched, constructed = [], []
        fetchall = cursor_class.fetchall
        deserialize = storage_module._deserialize_row

        async def counted_fetchall(cursor):
            rows = await fetchall(cursor)
            fetched.extend(rows)
            return rows

        def counted_deserialize(row):
            if "task_id" in row:
                constructed.append(row["task_id"])
            return deserialize(row)

        monkeypatch.setattr(cursor_class, "fetchall", counted_fetchall)
        monkeypatch.setattr(storage_module, "_deserialize_row", counted_deserialize)
        grouped = await storage.list_agent_tasks_for_sessions(keys)
        assert len(fetched) == len(constructed) == 1000
        assert {key: [task.task_id for task in rows] for key, rows in grouped.items()} == expected
        assert all(task.details is None for rows in grouped.values() for task in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [-1, 0, 1, 10, 100])
async def test_task_summary_alias_missing_and_per_key_limit(tmp_path, limit):
    key = "agent:main:webchat:default"
    async with SessionStorage(str(tmp_path / "sessions.db")) as storage:
        await seed_tasks(storage, [key], 12)
        assert await storage.list_agent_tasks_for_sessions([]) == {}
        grouped = await storage.list_agent_tasks_for_sessions(
            ["webchat:default", key, "agent:main:webchat:missing"], limit_per_session=limit,
        )
        assert list(grouped) == [key, "agent:main:webchat:missing"]
        assert grouped["agent:main:webchat:missing"] == []
        assert [task.task_id for task in grouped[key]] == [
            f"{key}-{index:04d}" for index in reversed(range(12))
        ][:max(0, limit)]


@pytest.mark.asyncio
async def test_task_summary_crosses_binding_chunk_without_losing_sessions(tmp_path, monkeypatch):
    keys = [f"agent:main:webchat:chunk-{i:04d}" for i in range(901)]
    async with SessionStorage(str(tmp_path / "sessions.db")) as storage:
        await seed_tasks(storage, keys, 2)
        connection_class = type(storage.conn)
        execute = connection_class.execute
        parameter_counts = []

        def counted_execute(connection, sql, parameters=()):
            if "FROM agent_tasks" in sql:
                parameter_counts.append(len(parameters))
            return execute(connection, sql, parameters)

        monkeypatch.setattr(connection_class, "execute", counted_execute)
        grouped = await storage.list_agent_tasks_for_sessions(keys, limit_per_session=1)
        assert list(grouped) == keys
        assert all([task.task_id for task in grouped[key]] == [f"{key}-0001"] for key in keys)
        assert len(parameter_counts) == 2
        assert max(parameter_counts) <= 900


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{}, {"session-0": []}, {"session-0": ["A title"]}])
async def test_successful_title_batch_never_reads_individual_transcripts(result):
    sessions = [SessionNode(session_key=f"agent:main:webchat:{i}", session_id=f"session-{i}")
                for i in range(200)]
    storage = SimpleNamespace(
        list_user_transcript_content_batch=AsyncMock(return_value=result),
        get_transcript=AsyncMock(side_effect=AssertionError("unexpected per-session read")),
    )
    titles = await _list_transcript_titles(storage, sessions)
    assert titles == ({"session-0": "A title"} if result.get("session-0") else {})
    storage.list_user_transcript_content_batch.assert_awaited_once()
    storage.get_transcript.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", ["missing", "error"])
async def test_unavailable_title_batch_keeps_legacy_fallback(batch):
    storage = SimpleNamespace(get_transcript=AsyncMock(return_value=[
        SimpleNamespace(role="user", content="Fallback title"),
    ]))
    if batch == "error":
        storage.list_user_transcript_content_batch = AsyncMock(side_effect=RuntimeError("read"))
    sessions = [SessionNode(session_key="agent:main:webchat:legacy", session_id="legacy")]
    assert await _list_transcript_titles(storage, sessions) == {"legacy": "Fallback title"}
    storage.get_transcript.assert_awaited_once_with("legacy", limit=8)


@pytest.mark.asyncio
async def test_shared_callers_keep_latest_state_and_exact_subagent_details(tmp_path):
    key = "agent:main:webchat:shared-summary"
    async with SessionStorage(str(tmp_path / "sessions.db")) as storage:
        await storage.upsert_session(SessionNode(session_key=key, session_id="shared"))
        await seed_tasks(storage, [key], 120)
        await storage.append_transcript_entry(TranscriptEntry(
            session_key=key, session_id="shared", role="user", content="shared query fixture",
        ))
        projected = await _list_task_rows_by_session(SimpleNamespace(), storage, [key])
        assert len(projected[key]) == 100
        assert projected[key][0].task_id == f"{key}-0119"
        assert projected[key][0].details is None

        latest = await _list_latest_task_rows_for_sessions(
            session_manager=storage, session_keys=[key],
        )
        assert latest[key].task_id == f"{key}-0119"
        assert latest[key].details == {"evidence": "exact-only"}
        exact = await _list_latest_task_rows_for_sessions(
            session_manager=storage, session_keys=[key], task_ids_by_session={key: f"{key}-0000"},
        )
        assert exact[key].task_id == f"{key}-0000"
        assert exact[key].details == {"evidence": "exact-only"}

        registry = ToolRegistry()
        create_session_search_tool(storage, registry=registry)
        tool = registry.get("session_search")
        assert tool is not None
        result = json.loads(await tool.handler(query="fixture"))
        assert result["results"][0]["runStatus"] == "failed"
