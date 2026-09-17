from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from opensquilla.application.approval_queue import ApprovalQueue
from opensquilla.gateway import boot
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus
from opensquilla.session.storage import SessionStorage


def test_restart_recovery_expires_every_pending_approval_once() -> None:
    storage = SimpleNamespace(
        restart_abandoned_session_keys=(
            "agent:main:webchat:first",
            "agent:main:webchat:second",
        ),
        take_restart_abandoned_session_keys=Mock(
            return_value=(
                "agent:main:webchat:first",
                "agent:main:webchat:second",
            )
        ),
    )
    queue = Mock()
    queue.expire_all_pending.return_value = 3

    expired = boot._expire_restart_orphaned_approvals(storage, queue)

    assert expired == 3
    storage.take_restart_abandoned_session_keys.assert_called_once_with()
    queue.expire_all_pending.assert_called_once_with()


def test_restart_recovery_reports_global_cleanup_failure(
    monkeypatch,
) -> None:
    storage = SimpleNamespace(restart_abandoned_session_keys=())
    queue = Mock()
    queue.expire_all_pending.side_effect = RuntimeError("locked")
    logger = Mock()
    monkeypatch.setattr(boot, "log", logger)

    expired = boot._expire_restart_orphaned_approvals(storage, queue)

    assert expired == 0
    queue.expire_all_pending.assert_called_once_with()
    logger.exception.assert_called_once_with("approval.restart_recovery_failed")


@pytest.mark.asyncio
async def test_process_restart_terminalizes_task_and_its_orphaned_approval(
    tmp_path,
) -> None:
    session_key = "agent:main:webchat:crashed-approval"
    session_db = tmp_path / "sessions.db"
    approval_db = tmp_path / "approval_queue.sqlite"

    storage = await SessionStorage.open(str(session_db))
    await storage.create_agent_task(
        AgentTaskRecord(
            task_id="crashed-task",
            session_key=session_key,
            source_kind="webui",
            queue_mode="followup",
            run_kind="web_turn",
            status=AgentTaskStatus.RUNNING,
        )
    )
    await storage.close()

    queue = ApprovalQueue(db_path=str(approval_db))
    approval_id = queue.request(
        "exec",
        {
            "sessionKey": session_key,
            "approvalKind": "sandbox_elevation",
            "humanActionable": True,
        },
    )
    unrelated_id = queue.request(
        "plugin",
        {
            "sessionKey": "agent:main:webchat:already-terminal",
            "pluginId": "example",
        },
    )
    unscoped_id = queue.request("plugin", {"pluginId": "unscoped"})
    queue.close()

    restarted_storage = await SessionStorage.open(str(session_db))
    restarted_queue = ApprovalQueue(db_path=str(approval_db))
    try:
        assert len(restarted_queue.list_pending()) == 3

        assert (
            boot._expire_restart_orphaned_approvals(
                restarted_storage,
                restarted_queue,
            )
            == 3
        )

        assert restarted_queue.list_pending() == []
        assert restarted_storage.restart_abandoned_session_keys == ()
        approval = restarted_queue.get(approval_id)
        assert approval.resolved is True
        assert approval.resolution == "expired"
        assert restarted_queue.get(unrelated_id).resolution == "expired"
        assert restarted_queue.get(unscoped_id).resolution == "expired"
        task = await restarted_storage.get_agent_task("crashed-task")
        assert task is not None
        assert task.status == AgentTaskStatus.ABANDONED
        assert task.terminal_reason == "process_restart"
    finally:
        restarted_queue.close()
        await restarted_storage.close()
