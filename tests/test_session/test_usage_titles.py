from __future__ import annotations

import json
import sqlite3

import pytest

from opensquilla.session.usage_titles import (
    resolve_usage_titles_on_conn,
    usage_metadata_title,
)


@pytest.mark.parametrize(
    ("display", "derived", "subject", "expected"),
    [
        ("WebChat", "制作我的世界复刻游戏", None, "制作我的世界复刻游戏"),
        ("Web chat", None, "Release review", "Release review"),
        ("Manual name", "Generated name", "Subject", "Manual name"),
        ("  ", "Generated name", "Subject", "Generated name"),
        ("WebChat", "I'm unable to help with this request.", "Subject", "Subject"),
        ("I cannot help with this request.", "Generated", None,
         "I cannot help with this request."),
        ("WebChat", None, None, ""),
        ("我的任务", "Generated", None, "我的任务"),
        ("Chat", "Generated", None, "Generated"),
    ],
)
def test_usage_title_metadata_priority(display, derived, subject, expected):
    assert usage_metadata_title({
        "session_key": "agent:main:webchat:fixture",
        "display_name": display, "derived_title": derived, "subject": subject,
    }) == expected


def test_usage_title_uses_agent_identity_from_non_main_key():
    assert usage_metadata_title({
        "session_key": "agent:reviewer:webchat:fixture", "agent_id": "main",
        "display_name": "reviewer", "derived_title": "Real review title",
    }) == "Real review title"


def test_non_webchat_generic_name_is_not_a_placeholder():
    assert usage_metadata_title({
        "session_key": "agent:main:telegram:fixture",
        "display_name": "WebChat", "derived_title": "Generated",
    }) == "WebChat"


class _Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.cursor.close()

    async def fetchall(self):
        return self.cursor.fetchall()


class _Connection:
    """Exercise the real SQLite queries without a threaded-driver fixture."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""
            CREATE TABLE sessions(session_id TEXT, epoch INTEGER);
            CREATE TABLE transcript_entries(
                id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                content TEXT, content_byte_length INTEGER, created_at INTEGER
            );
            CREATE TABLE compacted_transcript_entries(
                id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                content TEXT, content_byte_length INTEGER, created_at INTEGER,
                original_entry_id INTEGER
            );
        """)
        self.queries = []

    def execute(self, sql, params):
        self.queries.append(sql)
        return _Cursor(self.db.execute(sql, params))

    def message(self, session_id, content, *, byte_length=None):
        self.db.execute(
            "INSERT INTO transcript_entries VALUES (NULL, ?, 'user', ?, ?, 1)",
            (session_id, content, len(content.encode()) if byte_length is None else byte_length),
        )


@pytest.mark.asyncio
async def test_usage_transcript_fallback_is_bounded_and_generation_safe():
    conn = _Connection()
    try:
        conn.db.executemany("INSERT INTO sessions VALUES (?, ?)", [
            ("unnamed", 0), ("named", 0), ("reused", 1),
        ])
        conn.message("unnamed", "oversized old content", byte_length=70_000)
        conn.message("unnamed", json.dumps({
            "display_text": "", "attachments": [{"name": "private-filename.png"}],
        }))
        conn.message("unnamed", json.dumps({
            "display_text": "Review the release plan",
            "attachments": [{"name": "private-filename.png"}],
        }))
        conn.message("named", "A transcript must not override an explicit title")
        conn.message("reused", "New generation must never name an older task")
        nodes = [
            {"session_id": "unnamed", "epoch": 0, "session_key": "agent:main:webchat:u",
             "display_name": "WebChat"},
            {"session_id": "named", "epoch": 0, "session_key": "agent:main:webchat:n",
             "display_name": "Manual name"},
            {"session_id": "reused", "epoch": 0, "session_key": "agent:main:webchat:r",
             "display_name": "WebChat"},
        ]
        titles = await resolve_usage_titles_on_conn(conn, nodes)
        assert titles == {
            ("unnamed", 0): "Review the release plan",
            ("named", 0): "Manual name",
            ("reused", 0): "",
        }
        assert len(conn.queries) == 2
        assert "LIMIT 3" in conn.queries[0]
        assert "substr" in conn.queries[1]
    finally:
        conn.db.close()


@pytest.mark.asyncio
async def test_named_usage_titles_do_not_read_transcript_bodies():
    conn = _Connection()
    try:
        assert await resolve_usage_titles_on_conn(conn, [{
            "session_id": "named", "epoch": 0, "session_key": "agent:main:webchat:n",
            "display_name": "WebChat", "derived_title": "Generated title",
        }]) == {("named", 0): "Generated title"}
        assert conn.queries == []
    finally:
        conn.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_active_message", [False, True])
async def test_usage_transcript_title_keeps_canonical_first_message_after_compaction(
    has_active_message,
):
    conn = _Connection()
    try:
        conn.db.execute("INSERT INTO sessions VALUES ('compacted', 0)")
        conn.db.execute(
            "INSERT INTO compacted_transcript_entries VALUES "
            "(1, 'compacted', 'user', 'Original request', 16, 1, 10)",
        )
        if has_active_message:
            conn.db.execute(
                "INSERT INTO transcript_entries VALUES "
                "(11, 'compacted', 'user', 'Later follow-up', 15, 2)",
            )
        assert await resolve_usage_titles_on_conn(conn, [{
            "session_id": "compacted", "epoch": 0,
            "session_key": "agent:main:webchat:compacted", "display_name": "WebChat",
        }]) == {("compacted", 0): "Original request"}
    finally:
        conn.db.close()


@pytest.mark.asyncio
async def test_usage_title_does_not_read_past_three_unusable_candidates():
    conn = _Connection()
    try:
        conn.db.execute("INSERT INTO sessions VALUES ('bounded', 0)")
        for _ in range(3):
            conn.message("bounded", json.dumps({"display_text": "", "attachments": []}))
        conn.message("bounded", "Fourth message must not be used")
        titles = await resolve_usage_titles_on_conn(conn, [{
            "session_id": "bounded", "epoch": 0,
            "session_key": "agent:main:webchat:bounded", "display_name": "WebChat",
        }])
        assert titles == {("bounded", 0): ""}
    finally:
        conn.db.close()


@pytest.mark.asyncio
async def test_usage_title_resolution_batches_301_owners():
    conn = _Connection()
    try:
        nodes = []
        for index in range(301):
            session_id = f"task-{index}"
            conn.db.execute("INSERT INTO sessions VALUES (?, 0)", (session_id,))
            conn.message(session_id, f"Request {index}")
            nodes.append({
                "session_id": session_id, "epoch": 0,
                "session_key": f"agent:main:webchat:{session_id}", "display_name": "WebChat",
            })
        titles = await resolve_usage_titles_on_conn(conn, nodes)
        assert titles == {(f"task-{i}", 0): f"Request {i}" for i in range(301)}
        assert len(conn.queries) == 4  # candidate/body queries for each bounded batch
    finally:
        conn.db.close()
