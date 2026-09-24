"""Retired features cannot prevent upgrades or resume historical work."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from opensquilla.persistence.migrator import apply_pending
from opensquilla.session.models import AgentTaskRecord, SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


async def _legacy_database(path: Path) -> None:
    storage = await SessionStorage.open(str(path))
    session = SessionNode(session_key="agent:main:test:retirement", session_id="session-1")
    await storage.upsert_session(session)
    await storage.append_transcript_entry(TranscriptEntry(
        session_key=session.session_key, session_id=session.session_id,
        message_id="message-1", role="user", content="Keep this ordinary conversation.",
    ))
    await storage.create_agent_task(AgentTaskRecord(
        task_id="retired-task", session_key=session.session_key,
        details={"metadata": {"meta_control": {"kind": "manual"}}},
    ))
    await storage.close()
    backend = get_backend(f"sqlite:///{path}")
    migrations = read_migrations(str(MIGRATIONS)).filter(
        lambda migration: migration.id != "V047__retire_product_modes"
    )
    try:
        with backend.lock():
            backend.apply_migrations(backend.to_apply(migrations))
    finally:
        backend.connection.close()
    with sqlite3.connect(path) as conn:
        conn.execute("""
            INSERT INTO meta_skill_runs (
                run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json,
                triggered_by, session_key, status, started_at_ms, inputs_json, final_text
            ) VALUES ('run-1', 'synthetic-workflow', 'digest', '{}', 'manual_command',
                      'agent:main:test:retirement', 'ok', 1, '{}', 'Historical result')
        """)
        conn.execute("""
            INSERT INTO meta_skill_run_steps (
                run_id, step_id, step_kind, declared_skill, effective_skill,
                status, started_at_ms, rendered_inputs_json, output_text
            ) VALUES ('run-1', 'step-1', 'agent', 'ordinary', 'ordinary',
                      'ok', 1, '{}', 'Historical step')
        """)
        conn.execute("""
            INSERT INTO meta_launch_drafts (
                draft_id, session_key, client_request_id, meta_skill_name,
                launch_text, created_at, updated_at, expires_at
            ) VALUES ('draft-1', 'agent:main:test:retirement', 'request-1',
                      'synthetic-workflow', 'obsolete request', 1, 1, 9999999999999)
        """)


@pytest.mark.parametrize("versioned", [False, True])
async def test_upgrade_preserves_history_and_retires_execution(tmp_path, versioned):
    path = tmp_path / "sessions.db"
    await _legacy_database(path)
    if versioned:
        assert "V047__retire_product_modes" in apply_pending(str(path), MIGRATIONS)
    for _ in range(2):
        storage = await SessionStorage.open(str(path))
        try:
            task = await storage.get_agent_task("retired-task")
            assert task is not None
            assert task.status == "cancelled"
            assert task.terminal_reason == "feature_retired"
            async with storage.conn.execute("SELECT name FROM sqlite_master") as cursor:
                names = {row[0] for row in await cursor.fetchall()}
            assert not {"meta_control_intents", "meta_launch_drafts", "meta_skill_runs"} & names
            assert {"retired_meta_skill_runs", "retired_meta_skill_run_steps"} <= names
            async with storage.conn.execute(
                "SELECT final_text FROM retired_meta_skill_runs WHERE run_id = 'run-1'"
            ) as cursor:
                assert (await cursor.fetchone())[0] == "Historical result"
            async with storage.conn.execute(
                "SELECT output_text FROM retired_meta_skill_run_steps WHERE run_id = 'run-1'"
            ) as cursor:
                assert (await cursor.fetchone())[0] == "Historical step"
            async with storage.conn.execute(
                "SELECT content FROM transcript_entries WHERE message_id = 'message-1'"
            ) as cursor:
                assert (await cursor.fetchone())[0] == "Keep this ordinary conversation."
            async with storage.conn.execute("PRAGMA foreign_key_check") as cursor:
                assert await cursor.fetchall() == []
        finally:
            await storage.close()
    storage = await SessionStorage.open(str(path))
    try:
        await storage.delete_session("agent:main:test:retirement")
        for table in ("retired_meta_skill_runs", "retired_meta_skill_run_steps"):
            async with storage.conn.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                assert (await cursor.fetchone())[0] == 0
    finally:
        await storage.close()


async def test_new_database_does_not_create_discontinued_runtime_tables():
    storage = await SessionStorage.open(":memory:")
    try:
        async with storage.conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'meta_%'"
        ) as cursor:
            assert await cursor.fetchall() == []
    finally:
        await storage.close()


@pytest.mark.parametrize("ordinary_status", [None, "queued", "running"])
async def test_retirement_preserves_ordinary_work_in_a_shared_session(tmp_path, ordinary_status):
    path = tmp_path / "sessions.db"
    await _legacy_database(path)
    session_key = "agent:main:test:retirement"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET status = 'running' WHERE session_key = ?", (session_key,))
        if ordinary_status is not None:
            conn.execute("""
                INSERT INTO agent_tasks (
                    task_id, session_key, source_kind, queue_mode, status,
                    created_at, updated_at, details
                ) VALUES ('ordinary-task', ?, 'web', 'followup', ?, 1, 1, '{}')
            """, (session_key, ordinary_status))

    apply_pending(str(path), MIGRATIONS)

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT status FROM agent_tasks WHERE task_id = 'retired-task'"
        ).fetchone()[0] == "cancelled"
        ordinary = conn.execute(
            "SELECT status, finished_at, error_class FROM agent_tasks "
            "WHERE task_id = 'ordinary-task'"
        ).fetchone()
        if ordinary_status is None:
            assert ordinary is None
        else:
            assert ordinary == (ordinary_status, None, None)
        assert conn.execute(
            "SELECT status FROM sessions WHERE session_key = ?", (session_key,)
        ).fetchone()[0] == ("running" if ordinary_status else "killed")
        assert conn.execute("SELECT COUNT(*) FROM transcript_entries").fetchone()[0] == 1


async def test_direct_open_retirement_can_be_followed_by_versioned_upgrade(tmp_path):
    path = tmp_path / "sessions.db"
    await _legacy_database(path)
    storage = await SessionStorage.open(str(path))
    await storage.close()

    assert "V047__retire_product_modes" in apply_pending(str(path), MIGRATIONS)
    assert apply_pending(str(path), MIGRATIONS) == []
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM retired_meta_skill_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM retired_meta_skill_run_steps").fetchone()[0] == 1


@pytest.mark.parametrize("archived_steps", [False, True])
async def test_retirement_merges_live_and_archived_history_without_cascading_steps(
    tmp_path, archived_steps,
):
    path = tmp_path / "sessions.db"
    await _legacy_database(path)
    with sqlite3.connect(path) as conn:
        definitions = [conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,),
        ).fetchone()[0] for table in ("meta_skill_runs", "meta_skill_run_steps")]
    storage = await SessionStorage.open(str(path))
    await storage.close()

    # A partial prior upgrade may leave both archives and live tables. The
    # live child FK still points at the live parent until migration completes.
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        if not archived_steps:
            conn.execute("DROP TABLE retired_meta_skill_run_steps")
        for definition in definitions:
            conn.execute(definition)
        conn.execute("INSERT INTO meta_skill_runs SELECT * FROM retired_meta_skill_runs")
        conn.execute("UPDATE meta_skill_runs SET run_id = 'run-2'")
        conn.execute("""
            INSERT INTO meta_skill_run_steps (
                run_id, step_id, step_kind, declared_skill, effective_skill,
                status, started_at_ms, rendered_inputs_json, output_text
            ) VALUES ('run-2', 'step-2', 'agent', 'ordinary', 'ordinary',
                      'ok', 1, '{}', 'Second historical step')
        """)

    storage = await SessionStorage.open(str(path))
    try:
        async with storage.conn.execute("PRAGMA foreign_key_check") as cursor:
            assert await cursor.fetchall() == []
        async with storage.conn.execute("SELECT COUNT(*) FROM retired_meta_skill_runs") as cursor:
            assert (await cursor.fetchone())[0] == 2
        async with storage.conn.execute(
            "SELECT output_text FROM retired_meta_skill_run_steps WHERE run_id = 'run-2'"
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "Second historical step"
    finally:
        await storage.close()


async def test_partial_archive_child_foreign_key_is_retargeted_before_live_parent_drop(tmp_path):
    path = tmp_path / "sessions.db"
    await _legacy_database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        parent_schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'meta_skill_runs'"
        ).fetchone()[0]
        conn.execute(parent_schema.replace("meta_skill_runs", "retired_meta_skill_runs", 1))
        conn.execute("INSERT INTO retired_meta_skill_runs SELECT * FROM meta_skill_runs")
        conn.execute("ALTER TABLE meta_skill_run_steps RENAME TO retired_meta_skill_run_steps")

    storage = await SessionStorage.open(str(path))
    try:
        async with storage.conn.execute("PRAGMA foreign_key_check") as cursor:
            assert await cursor.fetchall() == []
        async with storage.conn.execute(
            "SELECT COUNT(*) FROM retired_meta_skill_run_steps"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 1
        await storage.delete_session("agent:main:test:retirement")
        async with storage.conn.execute(
            "SELECT COUNT(*) FROM retired_meta_skill_run_steps"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await storage.close()
