"""Storage contract tests for durable, idempotent turn acceptance."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from opensquilla.project_workspaces import (
    ProjectWorkspaceGuard,
    ProjectWorkspaceStateError,
)
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionNode,
    TranscriptEntry,
)
from opensquilla.session.storage import (
    SessionStorage,
    StorageBusyError,
    TurnIngressConflictError,
)

SESSION_KEY = "agent:main:webchat:durable-acceptance"
SESSION_ID = "session-durable-acceptance"


def _session(*, updated_at: int = 100) -> SessionNode:
    return SessionNode(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        agent_id="main",
        created_at=100,
        updated_at=updated_at,
        epoch=0,
    )


def _entry(message_id: str, *, content: str = "hello", created_at: int = 200) -> TranscriptEntry:
    return TranscriptEntry(
        session_id=SESSION_ID,
        session_key=SESSION_KEY,
        message_id=message_id,
        role="user",
        content=content,
        created_at=created_at,
    )


def _task(task_id: str, *, updated_at: int = 200) -> AgentTaskRecord:
    return AgentTaskRecord(
        task_id=task_id,
        session_key=SESSION_KEY,
        agent_id="main",
        source_kind="webui",
        queue_mode="followup",
        run_kind="web_turn",
        status=AgentTaskStatus.QUEUED,
        created_at=updated_at,
        updated_at=updated_at,
    )


async def _accept_turn(
    storage: SessionStorage,
    *,
    message_id: str,
    task_id: str,
    request_id: str = "request-one",
    fingerprint: str = "sha256:request-one",
    updated_at: int = 200,
) -> Any:
    return await storage.accept_turn(
        _entry(message_id, created_at=updated_at),
        expected_epoch=0,
        updated_at=updated_at,
        task_record=_task(task_id, updated_at=updated_at),
        source_scope="webui",
        request_session_key=SESSION_KEY,
        client_request_id=request_id,
        request_fingerprint=fingerprint,
    )


def _result_value(result: Any, name: str) -> Any:
    """Read an accepted identifier from either a result or its receipt member."""

    if isinstance(result, dict):
        candidate = result.get("receipt", result)
    else:
        candidate = getattr(result, "receipt", result)
    if isinstance(candidate, dict):
        return candidate[name]
    return getattr(candidate, name)


async def _row_count(storage: SessionStorage, table: str) -> int:
    async with storage.conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _receipt_rows(storage: SessionStorage) -> list[dict[str, Any]]:
    async with storage.conn.execute(
        "SELECT * FROM turn_ingress_receipts ORDER BY accepted_at, receipt_id"
    ) as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


@pytest.mark.asyncio
async def test_accept_turn_commits_message_session_task_and_receipt_together(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())

        result = await _accept_turn(
            storage,
            message_id="message-one",
            task_id="task-one",
        )

        transcript = await storage.get_transcript(SESSION_ID)
        session = await storage.get_session(SESSION_KEY)
        task = await storage.get_agent_task("task-one")
        receipts = await _receipt_rows(storage)

        assert [entry.message_id for entry in transcript] == ["message-one"]
        assert session is not None
        assert session.updated_at == 200
        assert task is not None
        assert task.status == AgentTaskStatus.QUEUED
        assert task.details is not None
        assert task.details["persisted_user_message_id"] == "message-one"
        assert task.details["persisted_user_message_ids"] == ["message-one"]
        assert task.details["message_count"] == 1
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt["receipt_id"]
        assert receipt["accepted_at"] >= 200
        assert {
            key: receipt[key]
            for key in (
                "source_scope",
                "request_session_key",
                "client_request_id",
                "request_fingerprint",
                "accepted_session_key",
                "session_id",
                "message_id",
                "task_id",
                "schema_version",
            )
        } == {
            "source_scope": "webui",
            "request_session_key": SESSION_KEY,
            "client_request_id": "request-one",
            "request_fingerprint": "sha256:request-one",
            "accepted_session_key": SESSION_KEY,
            "session_id": SESSION_ID,
            "message_id": "message-one",
            "task_id": "task-one",
            "schema_version": 1,
        }
        assert _result_value(result, "message_id") == "message-one"
        assert _result_value(result, "task_id") == "task-one"
        assert _result_value(result, "session_id") == SESSION_ID
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_updates_session_origin_in_same_transaction(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        session = _session()
        session.origin = {
            "surface": "webchat",
            "sandbox_run_context": {
                "run_mode": "trusted",
                "run_mode_source": "operator_default",
            },
        }
        await storage.upsert_session(session)
        selected_origin = {
            "surface": "webchat",
            "sandbox_run_context": {
                "run_mode": "standard",
                "run_mode_source": "user",
            },
        }

        await storage.accept_turn(
            _entry("message-mode"),
            expected_epoch=0,
            updated_at=200,
            task_record=_task("task-mode"),
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id="request-mode",
            request_fingerprint="sha256:request-mode",
            session_updates={"origin": selected_origin},
        )

        persisted = await storage.get_session(SESSION_KEY)
        assert persisted is not None
        assert persisted.origin == selected_origin
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_rolls_back_session_origin_with_failed_task_insert(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        original_origin = {
            "sandbox_run_context": {
                "run_mode": "trusted",
                "run_mode_source": "operator_default",
            }
        }
        session = _session()
        session.origin = original_origin
        await storage.upsert_session(session)
        await storage.conn.execute(
            """
            CREATE TRIGGER fail_mode_task_insert
            BEFORE INSERT ON agent_tasks
            BEGIN
                SELECT RAISE(ABORT, 'injected mode acceptance failure');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="injected mode acceptance failure"):
            await storage.accept_turn(
                _entry("message-mode-failed"),
                expected_epoch=0,
                updated_at=200,
                task_record=_task("task-mode-failed"),
                source_scope="webui",
                request_session_key=SESSION_KEY,
                client_request_id="request-mode-failed",
                request_fingerprint="sha256:request-mode-failed",
                session_updates={
                    "origin": {
                        "sandbox_run_context": {
                            "run_mode": "full",
                            "run_mode_source": "user",
                        }
                    }
                },
            )

        persisted = await storage.get_session(SESSION_KEY)
        assert persisted is not None
        assert persisted.origin == original_origin
        assert await storage.count_transcript_entries(SESSION_ID) == 0
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_table", ["agent_tasks", "turn_ingress_receipts"])
async def test_accept_turn_rolls_back_every_write_when_an_insert_fails(
    tmp_path,
    failing_table: str,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / f"{failing_table}.db"))
    try:
        await storage.upsert_session(_session())
        trigger_name = f"fail_acceptance_insert_{failing_table}"
        await storage.conn.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE INSERT ON {failing_table}
            BEGIN
                SELECT RAISE(ABORT, 'injected acceptance failure');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="injected acceptance failure"):
            await _accept_turn(
                storage,
                message_id="message-failed",
                task_id="task-failed",
            )

        session = await storage.get_session(SESSION_KEY)
        assert session is not None
        assert session.updated_at == 100
        assert await storage.count_transcript_entries(SESSION_ID) == 0
        assert await storage.get_agent_task("task-failed") is None
        assert await _row_count(storage, "turn_ingress_receipts") == 0
        assert storage.conn.in_transaction is False
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_replays_same_request_without_duplicate_side_effects(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        first = await _accept_turn(
            storage,
            message_id="message-original",
            task_id="task-original",
        )

        replay = await _accept_turn(
            storage,
            message_id="message-prospective-retry",
            task_id="task-prospective-retry",
            updated_at=300,
        )

        session = await storage.get_session(SESSION_KEY)
        assert session is not None
        assert session.updated_at == 200
        assert await storage.count_transcript_entries(SESSION_ID) == 1
        assert await _row_count(storage, "agent_tasks") == 1
        assert await _row_count(storage, "turn_ingress_receipts") == 1
        assert await storage.get_agent_task("task-prospective-retry") is None
        assert _result_value(replay, "receipt_id") == _result_value(first, "receipt_id")
        assert _result_value(replay, "message_id") == "message-original"
        assert _result_value(replay, "task_id") == "task-original"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_collects_into_existing_task_in_the_same_transaction(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        existing = _task("task-collect")
        existing.queue_mode = "collect"
        existing.details = {
            "message_count": 1,
            "persisted_user_message_id": "message-first",
            "persisted_user_message_ids": ["message-first"],
            "fresh_user_session": True,
            "existing_only": "preserved",
        }
        await storage.create_agent_task(existing)

        collected = _task("task-collect", updated_at=300)
        collected.queue_mode = "collect"
        collected.details = {
            "collected": True,
            "message_count": 2,
            "persisted_user_message_id": "message-first",
            "persisted_user_message_ids": [
                "message-first",
                "message-collected",
            ],
        }
        result = await storage.accept_turn(
            _entry("message-collected", content="second", created_at=300),
            expected_epoch=0,
            updated_at=300,
            task_record=collected,
            source_scope="webui",
            request_session_key=SESSION_KEY,
            client_request_id="request-collect",
            request_fingerprint="sha256:request-collect",
            merge_into_task=True,
        )

        task = await storage.get_agent_task("task-collect")
        assert task is not None
        assert task.details is not None
        assert task.details["collected"] is True
        assert task.details["message_count"] == 2
        assert task.details["persisted_user_message_id"] == "message-first"
        assert task.details["persisted_user_message_ids"] == [
            "message-first",
            "message-collected",
        ]
        assert task.details["fresh_user_session"] is True
        assert task.details["existing_only"] == "preserved"
        assert [
            entry.message_id for entry in await storage.get_transcript(SESSION_ID)
        ] == ["message-collected"]
        assert await _row_count(storage, "agent_tasks") == 1
        assert await _row_count(storage, "turn_ingress_receipts") == 1
        assert _result_value(result, "task_id") == "task-collect"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_failed_collected_acceptance_rolls_back_task_details_and_message(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        existing = _task("task-collect")
        existing.queue_mode = "collect"
        original_details = {
            "message_count": 1,
            "persisted_user_message_id": "message-first",
            "persisted_user_message_ids": ["message-first"],
        }
        existing.details = original_details
        await storage.create_agent_task(existing)
        await storage.conn.execute(
            """
            CREATE TRIGGER fail_collected_receipt
            BEFORE INSERT ON turn_ingress_receipts
            BEGIN
                SELECT RAISE(ABORT, 'injected collected receipt failure');
            END
            """
        )
        collected = _task("task-collect", updated_at=300)
        collected.queue_mode = "collect"
        collected.details = {"collected": True, "message_count": 2}

        with pytest.raises(sqlite3.IntegrityError, match="collected receipt failure"):
            await storage.accept_turn(
                _entry("message-collected", content="second", created_at=300),
                expected_epoch=0,
                updated_at=300,
                task_record=collected,
                source_scope="webui",
                request_session_key=SESSION_KEY,
                client_request_id="request-collect",
                request_fingerprint="sha256:request-collect",
                merge_into_task=True,
            )

        task = await storage.get_agent_task("task-collect")
        assert task is not None
        assert task.details == original_details
        assert await storage.get_transcript(SESSION_ID) == []
        assert await _row_count(storage, "turn_ingress_receipts") == 0
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_rejects_request_id_reuse_with_a_different_fingerprint(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(_session())
        await _accept_turn(
            storage,
            message_id="message-original",
            task_id="task-original",
        )

        with pytest.raises(Exception) as caught:
            await _accept_turn(
                storage,
                message_id="message-conflict",
                task_id="task-conflict",
                fingerprint="sha256:different-payload",
                updated_at=300,
            )

        assert caught.value.__class__.__name__ == "TurnIngressConflictError"
        session = await storage.get_session(SESSION_KEY)
        assert session is not None
        assert session.updated_at == 200
        assert await storage.count_transcript_entries(SESSION_ID) == 1
        assert await _row_count(storage, "agent_tasks") == 1
        assert await _row_count(storage, "turn_ingress_receipts") == 1
        assert await storage.get_agent_task("task-conflict") is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_rechecks_project_guard_in_transaction(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        node = _session()
        node.workspace_id = project.workspace_id
        guard = ProjectWorkspaceGuard(
            project.workspace_id,
            project.path,
            project.path_key,
        )
        await storage.remove_project_workspace(project.workspace_id)

        with pytest.raises(ProjectWorkspaceStateError) as raised:
            await storage.accept_turn(
                _entry("guarded-message"),
                expected_epoch=0,
                updated_at=200,
                task_record=None,
                source_scope="web:test",
                request_session_key=node.session_key,
                client_request_id="req-guarded",
                request_fingerprint="sha256:guarded",
                session_node=node,
                workspace_guard=guard,
            )
        assert raised.value.reason == "removed"
        assert await storage.get_session(node.session_key) is None
        assert await storage.get_turn_ingress_receipt(
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="req-guarded",
        ) is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_rejects_project_binding_mismatch(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        first_path = tmp_path / "first"
        second_path = tmp_path / "second"
        first_path.mkdir()
        second_path.mkdir()
        first = await storage.create_or_restore_project_workspace(
            path=str(first_path.resolve()),
            path_key=str(first_path.resolve()),
            display_name="first",
            trusted_at=1,
        )
        second = await storage.create_or_restore_project_workspace(
            path=str(second_path.resolve()),
            path_key=str(second_path.resolve()),
            display_name="second",
            trusted_at=1,
        )
        node = _session()
        node.workspace_id = first.workspace_id
        guard = ProjectWorkspaceGuard(
            second.workspace_id,
            second.path,
            second.path_key,
        )

        with pytest.raises(ProjectWorkspaceStateError) as raised:
            await storage.accept_turn(
                _entry("binding-mismatch"),
                expected_epoch=0,
                updated_at=200,
                task_record=None,
                source_scope="web:test",
                request_session_key=node.session_key,
                client_request_id="req-binding-mismatch",
                request_fingerprint="sha256:binding-mismatch",
                session_node=node,
                workspace_guard=guard,
            )

        assert raised.value.reason == "binding_changed"
        assert await storage.get_session(node.session_key) is None
        assert await storage.get_turn_ingress_receipt(
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="req-binding-mismatch",
        ) is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_requires_guard_for_bound_session(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        node = _session()
        node.workspace_id = project.workspace_id

        with pytest.raises(ProjectWorkspaceStateError) as raised:
            await storage.accept_turn(
                _entry("missing-guard"),
                expected_epoch=0,
                updated_at=200,
                task_record=None,
                source_scope="web:test",
                request_session_key=node.session_key,
                client_request_id="req-missing-guard",
                request_fingerprint="sha256:missing-guard",
                session_node=node,
            )

        assert raised.value.reason == "guard_required"
        assert await storage.get_session(node.session_key) is None
        assert await storage.get_turn_ingress_receipt(
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="req-missing-guard",
        ) is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_without_task_persists_nullable_receipt(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        node = _session()
        node.workspace_id = project.workspace_id
        guard = ProjectWorkspaceGuard(
            project.workspace_id,
            project.path,
            project.path_key,
        )

        accepted = await storage.accept_turn(
            _entry("taskless-message"),
            expected_epoch=0,
            updated_at=200,
            task_record=None,
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="req-taskless",
            request_fingerprint="sha256:taskless",
            session_node=node,
            workspace_guard=guard,
        )

        assert accepted.replayed is False
        assert accepted.receipt.task_id is None
        assert [
            item.message_id for item in await storage.get_transcript(node.session_id)
        ] == ["taskless-message"]
        receipt = await storage.get_turn_ingress_receipt(
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="req-taskless",
        )
        assert receipt is not None
        assert receipt.receipt.task_id is None
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_replays_before_removed_project_guard(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        node = _session()
        node.workspace_id = project.workspace_id
        guard = ProjectWorkspaceGuard(
            project.workspace_id,
            project.path,
            project.path_key,
        )
        first = await storage.accept_turn(
            _entry("original-project-message"),
            expected_epoch=0,
            updated_at=200,
            task_record=None,
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="req-project-replay",
            request_fingerprint="sha256:project-replay",
            session_node=node,
            workspace_guard=guard,
        )
        await storage.remove_project_workspace(project.workspace_id)

        replay = await storage.accept_turn(
            _entry("prospective-replay"),
            expected_epoch=0,
            updated_at=300,
            task_record=None,
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="req-project-replay",
            request_fingerprint="sha256:project-replay",
            session_node=node,
            workspace_guard=guard,
        )

        assert replay.replayed is True
        assert replay.receipt.receipt_id == first.receipt.receipt_id
        with pytest.raises(TurnIngressConflictError):
            await storage.accept_turn(
                _entry("conflicting-replay"),
                expected_epoch=0,
                updated_at=300,
                task_record=None,
                source_scope="web:test",
                request_session_key=node.session_key,
                client_request_id="req-project-replay",
                request_fingerprint="sha256:changed",
                session_node=node,
                workspace_guard=guard,
            )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_accept_turn_busy_timeout_has_no_partial_side_effects(tmp_path) -> None:
    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    locker: sqlite3.Connection | None = None
    try:
        await storage.upsert_session(_session())
        storage._busy_budget_seconds = 0.0
        locker = sqlite3.connect(str(db_path), timeout=0.1, isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")

        with pytest.raises(StorageBusyError):
            await _accept_turn(
                storage,
                message_id="message-busy",
                task_id="task-busy",
            )

        locker.execute("ROLLBACK")
        locker.close()
        locker = None

        session = await storage.get_session(SESSION_KEY)
        assert session is not None
        assert session.updated_at == 100
        assert await storage.count_transcript_entries(SESSION_ID) == 0
        assert await storage.get_agent_task("task-busy") is None
        assert await _row_count(storage, "turn_ingress_receipts") == 0
        assert storage.conn.in_transaction is False
    finally:
        if locker is not None:
            locker.execute("ROLLBACK")
            locker.close()
        await storage.close()


@pytest.mark.asyncio
async def test_project_accept_after_history_delete_commit_remains_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "sessions.db"
    deleting_storage = await SessionStorage.open(str(db_path))
    accepting_storage = await SessionStorage.open(str(db_path))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await deleting_storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        old = SessionNode(
            session_key="agent:main:webchat:history-old",
            workspace_id=project.workspace_id,
            created_at=100,
            updated_at=100,
        )
        await deleting_storage.upsert_session(old)
        new = SessionNode(
            session_key="agent:main:webchat:history-new",
            workspace_id=project.workspace_id,
            created_at=200,
            updated_at=200,
        )
        entry = TranscriptEntry(
            session_id=new.session_id,
            session_key=new.session_key,
            message_id="history-new-message",
            role="user",
            content="new history",
            created_at=200,
        )
        guard = ProjectWorkspaceGuard(
            project.workspace_id,
            project.path,
            project.path_key,
        )
        delete_entered = asyncio.Event()
        release_delete = asyncio.Event()
        accept_begin_attempted = asyncio.Event()
        original_delete_rows = deleting_storage._delete_session_rows
        original_begin = accepting_storage._begin_immediate

        async def paused_delete_rows(
            conn: Any,
            session: SessionNode,
        ) -> None:
            delete_entered.set()
            await release_delete.wait()
            await original_delete_rows(conn, session)

        async def observed_accept_begin(
            conn: Any,
            operation: str,
            deadline: float,
            started: float,
        ) -> None:
            if operation == "accept_turn":
                accept_begin_attempted.set()
            await original_begin(conn, operation, deadline, started)

        monkeypatch.setattr(
            deleting_storage,
            "_delete_session_rows",
            paused_delete_rows,
        )
        monkeypatch.setattr(
            accepting_storage,
            "_begin_immediate",
            observed_accept_begin,
        )
        deleting = asyncio.create_task(
            deleting_storage.delete_project_workspace_sessions(
                project.workspace_id,
                expected_session_keys=[old.session_key],
            )
        )
        await asyncio.wait_for(delete_entered.wait(), timeout=2)
        accepting = asyncio.create_task(
            accepting_storage.accept_turn(
                entry,
                expected_epoch=0,
                updated_at=200,
                task_record=None,
                source_scope="web:test",
                request_session_key=new.session_key,
                client_request_id="history-new-request",
                request_fingerprint="sha256:history-new-request",
                session_node=new,
                workspace_guard=guard,
            )
        )
        await asyncio.wait_for(accept_begin_attempted.wait(), timeout=2)
        await asyncio.sleep(0)
        assert accepting.done() is False
        release_delete.set()

        assert await deleting == [old.session_key]
        await accepting
        assert await accepting_storage.get_session(old.session_key) is None
        assert await accepting_storage.get_session(new.session_key) is not None
        assert [
            item.message_id
            for item in await accepting_storage.get_transcript(new.session_id)
        ] == ["history-new-message"]
    finally:
        await accepting_storage.close()
        await deleting_storage.close()


@pytest.mark.asyncio
async def test_project_accept_committed_before_history_delete_is_in_deleted_snapshot(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        node = SessionNode(
            session_key="agent:main:webchat:history-before-delete",
            workspace_id=project.workspace_id,
            created_at=100,
            updated_at=100,
        )
        guard = ProjectWorkspaceGuard(
            project.workspace_id,
            project.path,
            project.path_key,
        )
        await storage.accept_turn(
            TranscriptEntry(
                session_id=node.session_id,
                session_key=node.session_key,
                message_id="history-before-delete-message",
                role="user",
                content="old history",
                created_at=100,
            ),
            expected_epoch=0,
            updated_at=100,
            task_record=None,
            source_scope="web:test",
            request_session_key=node.session_key,
            client_request_id="history-before-delete-request",
            request_fingerprint="sha256:history-before-delete-request",
            session_node=node,
            workspace_guard=guard,
        )

        snapshot = await storage.list_project_workspace_session_keys(
            project.workspace_id
        )
        assert await storage.delete_project_workspace_sessions(
            project.workspace_id,
            expected_session_keys=snapshot,
        ) == [node.session_key]

        assert await storage.get_session(node.session_key) is None
        assert await storage.get_transcript(node.session_id) == []
        assert (
            await storage.get_turn_ingress_receipt(
                source_scope="web:test",
                request_session_key=node.session_key,
                client_request_id="history-before-delete-request",
            )
            is None
        )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_project_remove_commit_serializes_before_accept_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "sessions.db"
    removing_storage = await SessionStorage.open(str(db_path))
    accepting_storage = await SessionStorage.open(str(db_path))
    try:
        project_path = tmp_path / "project"
        project_path.mkdir()
        project = await removing_storage.create_or_restore_project_workspace(
            path=str(project_path.resolve()),
            path_key=str(project_path.resolve()),
            display_name="project",
            trusted_at=1,
        )
        node = SessionNode(
            session_key="agent:main:webchat:removed-before-accept",
            workspace_id=project.workspace_id,
            created_at=100,
            updated_at=100,
        )
        guard = ProjectWorkspaceGuard(
            project.workspace_id,
            project.path,
            project.path_key,
        )
        commit_entered = asyncio.Event()
        release_commit = asyncio.Event()
        accept_begin_attempted = asyncio.Event()
        original_commit = removing_storage._commit_transaction
        original_begin = accepting_storage._begin_immediate

        async def gated_commit(
            conn: Any,
            operation: str,
            deadline: float,
            started: float,
        ) -> None:
            if operation == "remove_project_workspace":
                commit_entered.set()
                await release_commit.wait()
            await original_commit(conn, operation, deadline, started)

        async def observed_accept_begin(
            conn: Any,
            operation: str,
            deadline: float,
            started: float,
        ) -> None:
            if operation == "accept_turn":
                accept_begin_attempted.set()
            await original_begin(conn, operation, deadline, started)

        monkeypatch.setattr(removing_storage, "_commit_transaction", gated_commit)
        monkeypatch.setattr(
            accepting_storage,
            "_begin_immediate",
            observed_accept_begin,
        )
        removing = asyncio.create_task(
            removing_storage.remove_project_workspace(project.workspace_id)
        )
        await asyncio.wait_for(commit_entered.wait(), timeout=2)
        accepting = asyncio.create_task(
            accepting_storage.accept_turn(
                TranscriptEntry(
                    session_id=node.session_id,
                    session_key=node.session_key,
                    message_id="removed-before-accept-message",
                    role="user",
                    content="must not persist",
                    created_at=100,
                ),
                expected_epoch=0,
                updated_at=100,
                task_record=None,
                source_scope="web:test",
                request_session_key=node.session_key,
                client_request_id="removed-before-accept-request",
                request_fingerprint="sha256:removed-before-accept-request",
                session_node=node,
                workspace_guard=guard,
            )
        )
        await asyncio.wait_for(accept_begin_attempted.wait(), timeout=2)
        await asyncio.sleep(0)
        assert accepting.done() is False
        release_commit.set()
        await removing

        with pytest.raises(ProjectWorkspaceStateError) as raised:
            await accepting
        assert raised.value.reason == "removed"
        assert await accepting_storage.get_session(node.session_key) is None
        assert await accepting_storage.get_transcript(node.session_id) == []
        assert (
            await accepting_storage.get_turn_ingress_receipt(
                source_scope="web:test",
                request_session_key=node.session_key,
                client_request_id="removed-before-accept-request",
            )
            is None
        )
    finally:
        await accepting_storage.close()
        await removing_storage.close()
