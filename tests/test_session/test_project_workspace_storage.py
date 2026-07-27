from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from opensquilla.persistence.migrator import apply_pending
from opensquilla.persistence.router_decision_writer import (
    open_router_decision_writer,
)
from opensquilla.persistence.turn_error_writer import open_turn_error_writer
from opensquilla.project_workspaces import ProjectWorkspaceGuard
from opensquilla.session.models import (
    AgentTaskRecord,
    MemoryDurableReceipt,
    SessionContextState,
    SessionNode,
    TranscriptEntry,
)
from opensquilla.session.storage import SessionStorage
from opensquilla.session.usage_ledger import UsageEventStart

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


async def _count_rows_for_session_key(
    storage: SessionStorage,
    table: str,
    session_key: str,
    *,
    column: str = "session_key",
) -> int:
    async with storage.conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",
        (session_key,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.asyncio
async def test_legacy_project_adoption_cas_does_not_create_or_bind_stale_candidate(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        original_origin = {
            "sandbox_run_context": {
                "run_mode": "standard",
                "workspace": str(tmp_path / "legacy"),
            }
        }
        session = SessionNode(
            session_key="agent:main:webchat:stale-adoption",
            origin=original_origin,
        )
        await storage.upsert_session(session)
        session.origin = {
            "sandbox_run_context": {
                "run_mode": "standard",
                "workspace": str(tmp_path / "changed"),
            }
        }
        await storage.upsert_session(session)

        adopted = await storage.adopt_legacy_session_workspace(
            session.session_key,
            expected_agent_id="main",
            expected_origin=original_origin,
            path=str(tmp_path / "legacy"),
            path_key=str(tmp_path / "legacy"),
            display_name="legacy",
            trusted_at=100,
            now_ms=100,
        )

        persisted = await storage.get_session(session.session_key)
        assert adopted is None
        assert persisted is not None and persisted.workspace_id is None
        assert await storage.list_project_workspaces() == []
    finally:
        await storage.close()


async def _seed_session_history(
    storage: SessionStorage,
    session: SessionNode,
    guard: ProjectWorkspaceGuard,
    *,
    suffix: str,
) -> None:
    await storage.accept_turn(
        TranscriptEntry(
            session_id=session.session_id,
            session_key=session.session_key,
            message_id=f"message-{suffix}",
            role="user",
            content=f"history-{suffix}",
            created_at=200,
        ),
        expected_epoch=0,
        updated_at=200,
        task_record=AgentTaskRecord(
            task_id=f"task-{suffix}",
            session_key=session.session_key,
            agent_id="main",
            created_at=200,
            updated_at=200,
        ),
        source_scope="workspace-history-test",
        request_session_key=session.session_key,
        client_request_id=f"request-{suffix}",
        request_fingerprint=f"sha256:{suffix}",
        workspace_guard=guard,
    )
    await storage.save_context_state(
        SessionContextState(
            session_id=session.session_id,
            session_key=session.session_key,
            state_kind="current",
            payload={"suffix": suffix},
            created_at=200,
        )
    )
    await storage.save_context_state(
        SessionContextState(
            session_id=f"old-{session.session_id}",
            session_key=session.session_key,
            state_kind="old-reset-epoch",
            payload={"suffix": suffix},
            created_at=100,
            valid=False,
            invalid_reason="session_reset",
        )
    )
    await storage.upsert_memory_durable_receipt(
        MemoryDurableReceipt(
            receipt_id=f"memory-{suffix}",
            session_key=session.session_key,
            session_id=session.session_id,
            scope="test",
            idempotency_key=f"memory-{suffix}",
            status="committed",
            created_at=200,
            updated_at=200,
        )
    )


def _seed_observability_history(
    db_path: str,
    session_key: str,
    *,
    suffix: str,
) -> None:
    decision_writer = open_router_decision_writer(db_path)
    try:
        assert decision_writer.record_decision(
            {
                "decision_id": f"decision-{suffix}",
                "session_key": session_key,
                "ts_ms": 200,
                "executed_kind": "single",
            }
        )
    finally:
        decision_writer.close()

    error_writer = open_turn_error_writer(db_path)
    try:
        assert error_writer.record_error(
            {
                "error_id": f"error-{suffix}",
                "session_key": session_key,
                "session_id": f"session-{suffix}",
                "ts_ms": 200,
                "error_class": "test_error",
            }
        )
    finally:
        error_writer.close()


@pytest.mark.asyncio
async def test_project_workspaces_keep_fixed_unpinned_order(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        first = await storage.create_or_restore_project_workspace(
            path="/repo/first",
            path_key="/repo/first",
            display_name="first",
            trusted_at=100,
            now_ms=100,
        )
        second = await storage.create_or_restore_project_workspace(
            path="/repo/second",
            path_key="/repo/second",
            display_name="second",
            trusted_at=200,
            now_ms=100,
        )

        assert second.position_at > first.position_at
        rows = await storage.list_project_workspaces()
        assert [row.workspace_id for row in rows] == [
            second.workspace_id,
            first.workspace_id,
        ]

        await storage.update_project_workspace(first.workspace_id, display_name="renamed")
        assert [row.workspace_id for row in await storage.list_project_workspaces()] == [
            second.workspace_id,
            first.workspace_id,
        ]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_newly_pinned_workspace_leads_pinned_region(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        first = await storage.create_or_restore_project_workspace(
            path="/repo/first",
            path_key="/repo/first",
            display_name="first",
            trusted_at=100,
            now_ms=100,
        )
        second = await storage.create_or_restore_project_workspace(
            path="/repo/second",
            path_key="/repo/second",
            display_name="second",
            trusted_at=200,
            now_ms=200,
        )

        await storage.set_project_workspace_pin(first.workspace_id, pinned=True, now_ms=300)
        await storage.set_project_workspace_pin(second.workspace_id, pinned=True, now_ms=300)
        rows = await storage.list_project_workspaces()
        assert rows[0].pinned_at is not None
        assert rows[1].pinned_at is not None
        assert rows[0].pinned_at > rows[1].pinned_at
        assert [row.workspace_id for row in rows] == [
            second.workspace_id,
            first.workspace_id,
        ]

        await storage.set_project_workspace_pin(second.workspace_id, pinned=False, now_ms=500)
        rows = await storage.list_project_workspaces()
        assert [row.workspace_id for row in rows] == [
            first.workspace_id,
            second.workspace_id,
        ]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_remove_and_restore_reuses_workspace_identity(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        workspace = await storage.create_or_restore_project_workspace(
            path="/repo/project",
            path_key="/repo/project",
            display_name="project",
            trusted_at=100,
            now_ms=100,
        )
        await storage.remove_project_workspace(workspace.workspace_id, now_ms=200)
        assert await storage.list_project_workspaces() == []

        restored = await storage.create_or_restore_project_workspace(
            path="/repo/project",
            path_key="/repo/project",
            display_name="ignored-new-default",
            trusted_at=300,
            now_ms=300,
        )
        assert restored.workspace_id == workspace.workspace_id
        assert restored.display_name == "project"
        assert restored.removed_at is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_workspace_binding_and_history_delete_leave_project(tmp_path) -> None:
    db_path = str(tmp_path / "sessions.db")
    apply_pending(db_path, MIGRATIONS_DIR)
    storage = await SessionStorage.open(db_path)
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        sentinel = project_path / "keep-me.txt"
        sentinel.write_text("project data", encoding="utf-8")
        workspace = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=100,
            now_ms=100,
        )
        guard = ProjectWorkspaceGuard(
            workspace.workspace_id,
            workspace.path,
            workspace.path_key,
        )
        second = SessionNode(
            session_key="agent:main:webchat:history-b",
            workspace_id=workspace.workspace_id,
            created_at=100,
            updated_at=100,
            spawn_depth=1,
            spawned_by="agent:main:webchat:history-a",
            parent_session_key="agent:main:webchat:history-a",
        )
        first = SessionNode(
            session_key="agent:main:webchat:history-a",
            workspace_id=workspace.workspace_id,
            created_at=100,
            updated_at=100,
        )
        # Reverse key order on purpose: deletion order must use
        # (created_at, session_key), not insertion/rowid order.
        await storage.upsert_session(second)
        await storage.upsert_session(first)
        await _seed_session_history(storage, first, guard, suffix="target")
        _seed_observability_history(
            db_path,
            first.session_key,
            suffix="target",
        )
        await storage.start_usage_event(
            UsageEventStart(
                event_id="usage-target",
                execution_id="execution-target",
                call_index=0,
                session_id=first.session_id,
                started_at_ms=200,
            )
        )

        other_path = tmp_path / "other"
        other_path.mkdir()
        other_workspace = await storage.create_or_restore_project_workspace(
            path=str(other_path.resolve()),
            path_key=str(other_path.resolve()),
            display_name="other",
            trusted_at=100,
            now_ms=100,
        )
        other_guard = ProjectWorkspaceGuard(
            other_workspace.workspace_id,
            other_workspace.path,
            other_workspace.path_key,
        )
        other = SessionNode(
            session_key="agent:main:webchat:history-other",
            workspace_id=other_workspace.workspace_id,
            created_at=100,
            updated_at=100,
        )
        await storage.upsert_session(other)
        await _seed_session_history(storage, other, other_guard, suffix="other")
        _seed_observability_history(
            db_path,
            other.session_key,
            suffix="other",
        )

        assert await storage.count_project_workspace_tasks(workspace.workspace_id) == 1
        deleted = await storage.delete_project_workspace_sessions(workspace.workspace_id)
        assert deleted == [
            "agent:main:webchat:history-a",
            "agent:main:webchat:history-b",
        ]
        for session_key in deleted:
            assert await storage.get_session(session_key) is None
        for table, column, other_count in (
            ("transcript_entries", "session_key", 1),
            ("session_context_states", "session_key", 2),
            ("agent_tasks", "session_key", 1),
            ("memory_durable_receipts", "session_key", 1),
            ("turn_ingress_receipts", "accepted_session_key", 1),
            ("router_decisions", "session_key", 1),
            ("turn_errors", "session_key", 1),
        ):
            assert (
                await _count_rows_for_session_key(
                    storage,
                    table,
                    first.session_key,
                    column=column,
                )
                == 0
            )
            assert (
                await _count_rows_for_session_key(
                    storage,
                    table,
                    other.session_key,
                    column=column,
                )
                == other_count
            )
        retained_other = await storage.get_session(other.session_key)
        assert retained_other is not None
        assert retained_other.workspace_id == other_workspace.workspace_id
        async with storage.conn.execute(
            "SELECT event_id FROM usage_events WHERE session_id = ?",
            (first.session_id,),
        ) as cursor:
            assert [row[0] for row in await cursor.fetchall()] == ["usage-target"]
        assert await storage.get_project_workspace(workspace.workspace_id) is not None
        assert sentinel.read_text(encoding="utf-8") == "project data"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_project_history_delete_rolls_back_all_sessions_and_dependents(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "sessions.db")
    apply_pending(db_path, MIGRATIONS_DIR)
    storage = await SessionStorage.open(db_path)
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        sentinel = project_path / "keep-me.txt"
        sentinel.write_text("project data", encoding="utf-8")
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=100,
            now_ms=100,
        )
        guard = ProjectWorkspaceGuard(
            project.workspace_id,
            project.path,
            project.path_key,
        )
        first = SessionNode(
            session_key="agent:main:webchat:history-first",
            workspace_id=project.workspace_id,
            created_at=100,
            updated_at=100,
        )
        second = SessionNode(
            session_key="agent:main:webchat:history-second",
            workspace_id=project.workspace_id,
            created_at=200,
            updated_at=200,
        )
        await storage.upsert_session(first)
        await storage.upsert_session(second)
        await _seed_session_history(storage, first, guard, suffix="rollback")
        _seed_observability_history(
            db_path,
            first.session_key,
            suffix="rollback",
        )
        await storage.conn.execute(
            """
            CREATE TRIGGER fail_second_session_delete
            BEFORE DELETE ON sessions
            WHEN OLD.session_key = 'agent:main:webchat:history-second'
            BEGIN
                SELECT RAISE(ABORT, 'injected delete failure');
            END
            """
        )
        await storage.conn.commit()

        with pytest.raises(sqlite3.DatabaseError, match="injected delete failure"):
            await storage.delete_project_workspace_sessions(project.workspace_id)

        assert await storage.get_session(first.session_key) is not None
        assert await storage.get_session(second.session_key) is not None
        for table, column, expected in (
            ("transcript_entries", "session_key", 1),
            ("session_context_states", "session_key", 2),
            ("agent_tasks", "session_key", 1),
            ("memory_durable_receipts", "session_key", 1),
            ("turn_ingress_receipts", "accepted_session_key", 1),
            ("router_decisions", "session_key", 1),
            ("turn_errors", "session_key", 1),
        ):
            assert (
                await _count_rows_for_session_key(
                    storage,
                    table,
                    first.session_key,
                    column=column,
                )
                == expected
            )
        assert await storage.get_project_workspace(project.workspace_id) == project
        assert sentinel.read_text(encoding="utf-8") == "project data"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_project_history_delete_attempts_every_cleanup_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        sentinel = project_path / "keep-me.txt"
        sentinel.write_text("project data", encoding="utf-8")
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=100,
            now_ms=100,
        )
        sessions = [
            SessionNode(
                session_key=f"agent:main:webchat:cleanup-{suffix}",
                workspace_id=project.workspace_id,
                created_at=created_at,
                updated_at=created_at,
            )
            for suffix, created_at in (("first", 100), ("second", 200))
        ]
        for session in sessions:
            await storage.upsert_session(session)

        cleanup_calls: list[str] = []

        async def cleanup(session: SessionNode) -> None:
            cleanup_calls.append(session.session_key)
            if session == sessions[0]:
                raise RuntimeError("injected cleanup failure")

        monkeypatch.setattr(
            storage,
            "_cleanup_deleted_session",
            cleanup,
            raising=False,
        )

        deleted = await storage.delete_project_workspace_sessions(project.workspace_id)

        assert deleted == [session.session_key for session in sessions]
        assert cleanup_calls == deleted
        assert all(
            session is None
            for session in [await storage.get_session(key) for key in deleted]
        )
        assert sentinel.read_text(encoding="utf-8") == "project data"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_project_history_delete_cancellation_waits_for_commit_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=100,
            now_ms=100,
        )
        sessions = [
            SessionNode(
                session_key=f"agent:main:webchat:cancel-{suffix}",
                workspace_id=project.workspace_id,
                created_at=created_at,
                updated_at=created_at,
            )
            for suffix, created_at in (("first", 100), ("second", 200))
        ]
        for session in sessions:
            await storage.upsert_session(session)

        commit_entered = asyncio.Event()
        release_commit = asyncio.Event()
        original_commit = storage._commit_transaction

        async def gated_commit(
            conn: object,
            operation: str,
            deadline: float,
            started: float,
        ) -> None:
            if operation == "delete_project_workspace_sessions":
                commit_entered.set()
                await release_commit.wait()
            await original_commit(conn, operation, deadline, started)

        cleanup_calls: list[str] = []

        async def cleanup(session: SessionNode) -> None:
            cleanup_calls.append(session.session_key)

        monkeypatch.setattr(storage, "_commit_transaction", gated_commit)
        monkeypatch.setattr(
            storage,
            "_cleanup_deleted_session",
            cleanup,
            raising=False,
        )

        deleting = asyncio.create_task(
            storage.delete_project_workspace_sessions(project.workspace_id)
        )
        await asyncio.wait_for(commit_entered.wait(), timeout=2)
        deleting.cancel()
        await asyncio.sleep(0)
        release_commit.set()

        with pytest.raises(asyncio.CancelledError):
            await deleting

        expected_keys = [session.session_key for session in sessions]
        assert cleanup_calls == expected_keys
        assert all(
            session is None
            for session in [
                await storage.get_session(key)
                for key in expected_keys
            ]
        )
    finally:
        await storage.close()
