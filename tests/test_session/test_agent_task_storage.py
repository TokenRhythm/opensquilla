from __future__ import annotations

import pytest

from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus, SessionNode, SessionStatus
from opensquilla.session.storage import SessionStorage


@pytest.mark.asyncio
async def test_agent_task_ledger_marks_active_tasks_abandoned_after_restart(tmp_path) -> None:
    db_path = tmp_path / "sessions.db"
    key = "agent:main:webchat:restart-ledger"

    storage = SessionStorage(str(db_path))
    await storage.connect()
    try:
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="queued-task",
                session_key=key,
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.QUEUED,
                created_at=100,
                updated_at=100,
            )
        )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="running-task",
                session_key=key,
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.RUNNING,
                created_at=110,
                updated_at=120,
                started_at=120,
            )
        )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="done-task",
                session_key=key,
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.SUCCEEDED,
                created_at=130,
                updated_at=140,
                started_at=135,
                finished_at=140,
            )
        )
    finally:
        await storage.close()

    restarted = SessionStorage(str(db_path))
    await restarted.connect()
    try:
        rows = await restarted.list_agent_tasks(session_key=key)
    finally:
        await restarted.close()

    by_id = {row.task_id: row for row in rows}
    assert by_id["queued-task"].status == AgentTaskStatus.ABANDONED
    assert by_id["queued-task"].terminal_reason == "process_restart"
    assert by_id["queued-task"].finished_at is not None
    assert by_id["running-task"].status == AgentTaskStatus.ABANDONED
    assert by_id["running-task"].terminal_reason == "process_restart"
    assert by_id["running-task"].finished_at is not None
    assert by_id["done-task"].status == AgentTaskStatus.SUCCEEDED
    assert by_id["done-task"].terminal_reason is None


@pytest.mark.asyncio
async def test_agent_task_abandonment_terminalizes_owning_running_sessions(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    running_key = "agent:main:webchat:running-restart"
    terminal_key = "agent:main:webchat:done-restart"
    try:
        await storage.upsert_session(
            SessionNode(
                session_key=running_key,
                session_id="running-session",
                agent_id="main",
                created_at=1000,
                updated_at=1000,
                started_at=1000,
                status=SessionStatus.RUNNING,
            )
        )
        await storage.upsert_session(
            SessionNode(
                session_key=terminal_key,
                session_id="done-session",
                agent_id="main",
                created_at=1200,
                updated_at=1800,
                started_at=1200,
                ended_at=1800,
                runtime_ms=600,
                status=SessionStatus.DONE,
            )
        )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="running-task",
                session_key=running_key,
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.RUNNING,
                created_at=1100,
                updated_at=1100,
                started_at=1100,
            )
        )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="terminal-session-task",
                session_key=terminal_key,
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.RUNNING,
                created_at=1300,
                updated_at=1300,
                started_at=1300,
            )
        )

        abandoned = await storage.mark_abandoned_agent_tasks(now_ms=2500)

        running = await storage.get_session(running_key)
        terminal = await storage.get_session(terminal_key)
    finally:
        await storage.close()

    assert abandoned == 2
    assert running is not None
    assert running.status == SessionStatus.FAILED
    assert running.ended_at == 2500
    assert running.runtime_ms == 1500
    assert running.updated_at == 2500
    assert terminal is not None
    assert terminal.status == SessionStatus.DONE
    assert terminal.ended_at == 1800
    assert terminal.runtime_ms == 600


@pytest.mark.asyncio
async def test_agent_task_abandonment_repairs_previously_partial_restart_cleanup(
    tmp_path,
) -> None:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    key = "agent:main:webchat:partial-restart"
    try:
        await storage.upsert_session(
            SessionNode(
                session_key=key,
                session_id="partial-session",
                agent_id="main",
                created_at=1000,
                updated_at=1000,
                started_at=1000,
                status=SessionStatus.RUNNING,
            )
        )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="already-abandoned-task",
                session_key=key,
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.ABANDONED,
                created_at=1100,
                updated_at=1800,
                started_at=1100,
                finished_at=1800,
                terminal_reason="process_restart",
            )
        )

        abandoned = await storage.mark_abandoned_agent_tasks(now_ms=2500)

        node = await storage.get_session(key)
    finally:
        await storage.close()

    assert abandoned == 0
    assert node is not None
    assert node.status == SessionStatus.FAILED
    assert node.ended_at == 2500
    assert node.runtime_ms == 1500
    assert node.updated_at == 2500


@pytest.mark.asyncio
async def test_list_agent_tasks_for_sessions_groups_visible_session_tasks(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    try:
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="one-old",
                session_key="agent:main:webchat:one",
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.SUCCEEDED,
                created_at=100,
                updated_at=100,
            )
        )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="one-new",
                session_key="agent:main:webchat:one",
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.RUNNING,
                created_at=200,
                updated_at=200,
                details={"terminal_assistant_message_content": "x" * 100_000},
            )
        )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="two-task",
                session_key="agent:main:webchat:two",
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.QUEUED,
                created_at=150,
                updated_at=150,
            )
        )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="hidden-task",
                session_key="agent:main:webchat:hidden",
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.QUEUED,
                created_at=50,
                updated_at=50,
            )
        )

        grouped = await storage.list_agent_tasks_for_sessions(
            ["agent:main:webchat:one", "agent:main:webchat:two"],
            limit_per_session=1,
        )
        exact = await storage.get_agent_task("one-new")
    finally:
        await storage.close()

    assert set(grouped) == {"agent:main:webchat:one", "agent:main:webchat:two"}
    assert [row.task_id for row in grouped["agent:main:webchat:one"]] == ["one-new"]
    assert [row.task_id for row in grouped["agent:main:webchat:two"]] == ["two-task"]
    assert grouped["agent:main:webchat:one"][0].details is None
    assert exact is not None
    assert exact.details == {"terminal_assistant_message_content": "x" * 100_000}


@pytest.mark.asyncio
async def test_list_sessions_keeps_active_task_session_before_limit(tmp_path) -> None:
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    old_key = "agent:main:webchat:old-running"
    try:
        await storage.upsert_session(
            SessionNode(
                session_key=old_key,
                session_id="old-session",
                agent_id="main",
                created_at=1,
                updated_at=1,
                started_at=1,
                status=SessionStatus.RUNNING,
            )
        )
        for index in range(200):
            await storage.upsert_session(
                SessionNode(
                    session_key=f"agent:main:webchat:new-{index:03d}",
                    session_id=f"new-session-{index:03d}",
                    agent_id="main",
                    created_at=1000 + index,
                    updated_at=1000 + index,
                    started_at=1000 + index,
                    ended_at=2000 + index,
                    status=SessionStatus.DONE,
                )
            )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="task-running",
                session_key=old_key,
                source_kind="webui",
                queue_mode="followup",
                run_kind="web_turn",
                status=AgentTaskStatus.RUNNING,
                created_at=9999,
                updated_at=9999,
                started_at=9999,
            )
        )

        rows = await storage.list_sessions(limit=200)
    finally:
        await storage.close()

    keys = [row.session_key for row in rows]
    assert old_key in keys
    assert keys[0] == old_key
