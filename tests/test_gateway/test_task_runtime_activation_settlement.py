"""A committed atomic terminal settlement replaces the compatibility release."""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from opensquilla.application.approval_queue import ApprovalQueue
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus, SessionNode
from opensquilla.session.storage import AgentTaskSettlementReceipt, SessionStorage


@asynccontextmanager
async def _stack(tmp_path, *, storage_type=SessionStorage, handler=None):
    storage = await storage_type.open(str(tmp_path / "sessions.db"))
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.db"))
    node = SessionNode(session_key="agent:main:webchat:atomic-release", session_id="owner", epoch=0)
    await storage.upsert_session(node)
    operations = Counter()
    transaction = storage._write_transaction

    @asynccontextmanager
    async def count(operation, **kwargs):
        operations[operation] += 1
        async with transaction(operation, **kwargs) as conn:
            yield conn

    storage._write_transaction = count

    async def immediate(_run):
        pass

    runtime = TaskRuntime(
        storage=storage, turn_handler=handler or immediate,
        running_heartbeat_interval_s=None,
    )
    try:
        with patch("opensquilla.application.approval_queue.get_approval_queue", return_value=queue):
            yield storage, runtime, node, operations
    finally:
        await runtime.shutdown(timeout=2)
        await storage.close()
        queue.close()


def _envelope(node, *, authority=True):
    metadata = {}
    if authority:
        metadata["activation_authority"] = {
            "session_id": node.session_id,
            "session_epoch": node.epoch,
            "authorization_revision": 1,
            "permission_fingerprint": "desktop-loopback:operator.write:v1",
        }
    return RouteEnvelope(
        source_kind=SourceKind.WEB, source_name="test", agent_id="main",
        session_key=node.session_key, session_id=node.session_id, session_epoch=node.epoch,
        metadata=metadata, input_provenance={"kind": "test"},
    )


async def _drain(runtime, task_id):
    driver = runtime._tasks[task_id].asyncio_task
    assert driver is not None
    await asyncio.wait_for(asyncio.shield(driver), timeout=2)
    return await runtime.status(task_id)


@pytest.mark.parametrize("authority", [False, True])
async def test_atomic_terminal_receipt_skips_duplicate_release(tmp_path, authority):
    async with _stack(tmp_path) as (storage, runtime, node, operations):
        handle = await runtime.enqueue(_envelope(node, authority=authority), "finish")
        record = await _drain(runtime, handle.task_id)
        assert record.status == AgentTaskStatus.SUCCEEDED
        assert operations["settle_agent_task"] == 1
        assert operations["release_activation_permit"] == 0
        permit = await storage.get_activation_permit(handle.task_id)
        assert (permit.state if permit else None) == ("released" if authority else None)


class _NonAtomicStorage(SessionStorage):
    async def settle_agent_task(self, task_id, **fields):
        # A legacy override persists the row without settling the permit.
        update = {
            key: value for key, value in fields.items()
            if key not in {"session_key", "details_patch", "remove_detail_keys", "plan_result"}
        }
        return await self.update_agent_task(task_id, **update)


async def test_inherited_receipt_api_does_not_certify_legacy_settle_override(tmp_path):
    async with _stack(tmp_path, storage_type=_NonAtomicStorage) as (
        storage, runtime, node, operations,
    ):
        handle = await runtime.enqueue(_envelope(node), "finish")
        assert (await _drain(runtime, handle.task_id)).status == AgentTaskStatus.SUCCEEDED
        assert operations["release_activation_permit"] == 1
        assert (await storage.get_activation_permit(handle.task_id)).state == "released"


async def test_class_level_settle_override_does_not_receive_atomic_receipt(tmp_path, monkeypatch):
    async with _stack(tmp_path) as (storage, runtime, node, operations):
        monkeypatch.setattr(
            SessionStorage, "settle_agent_task", _NonAtomicStorage.settle_agent_task,
        )
        handle = await runtime.enqueue(_envelope(node), "finish")
        assert (await _drain(runtime, handle.task_id)).status == AgentTaskStatus.SUCCEEDED
        assert operations["release_activation_permit"] == 1
        assert (await storage.get_activation_permit(handle.task_id)).state == "released"


@pytest.mark.parametrize("receipt", [None, "wrong_type", "wrong_task", "wrong_session"])
async def test_unproved_settlement_keeps_compatibility_release(tmp_path, monkeypatch, receipt):
    async with _stack(tmp_path) as (storage, runtime, node, operations):
        original = storage.settle_agent_task_with_receipt

        async def altered(task_id, **fields):
            actual = await original(task_id, **fields)
            if receipt is None or receipt == "wrong_type":
                return receipt
            return AgentTaskSettlementReceipt(
                task_id="another-task" if receipt == "wrong_task" else actual.task_id,
                session_key="agent:main:webchat:other" if receipt == "wrong_session"
                else actual.session_key,
            )

        monkeypatch.setattr(storage, "settle_agent_task_with_receipt", altered)
        handle = await runtime.enqueue(_envelope(node), "finish")
        assert (await _drain(runtime, handle.task_id)).status == AgentTaskStatus.SUCCEEDED
        assert operations["release_activation_permit"] == 1
        assert (await storage.get_activation_permit(handle.task_id)).state == "released"


@pytest.mark.parametrize("failures", [1, 2])
async def test_failed_settlement_retains_compensation_and_retry_release(
    tmp_path, monkeypatch, failures,
):
    async with _stack(tmp_path) as (storage, runtime, node, operations):
        transaction = storage._write_transaction
        remaining = failures

        @asynccontextmanager
        async def fail_terminal(operation, **kwargs):
            nonlocal remaining
            if operation == "settle_agent_task" and remaining:
                remaining -= 1
                raise RuntimeError("synthetic terminal write failure")
            async with transaction(operation, **kwargs) as conn:
                yield conn

        monkeypatch.setattr(storage, "_write_transaction", fail_terminal)
        handle = await runtime.enqueue(_envelope(node), "finish")
        assert (await _drain(runtime, handle.task_id)).status == AgentTaskStatus.SUCCEEDED
        if failures == 2:
            assert runtime._terminal_retry_task is not None
            await asyncio.wait_for(asyncio.shield(runtime._terminal_retry_task), timeout=2)
        # A successful compensation proves atomic release; a queued retry did
        # not prove it yet, so terminal settlement still performs the fallback.
        assert operations["release_activation_permit"] == (1 if failures == 2 else 0)
        assert (await storage.get_agent_task(handle.task_id)).status == AgentTaskStatus.SUCCEEDED
        assert (await storage.get_activation_permit(handle.task_id)).state == "released"


async def test_running_and_queued_cancellation_leave_no_active_permit(tmp_path):
    started = asyncio.Event()
    never = asyncio.Event()

    async def handler(_run):
        started.set()
        await never.wait()

    async with _stack(tmp_path, handler=handler) as (storage, runtime, node, operations):
        running = await runtime.enqueue(_envelope(node), "running")
        await asyncio.wait_for(started.wait(), timeout=2)
        queued = await runtime.enqueue(_envelope(node), "queued")
        running_driver = runtime._tasks[running.task_id].asyncio_task
        queued_driver = runtime._tasks[queued.task_id].asyncio_task
        assert await runtime.cancel(task_id=queued.task_id) == 1
        assert await runtime.cancel(task_id=running.task_id) == 1
        await asyncio.wait_for(asyncio.gather(running_driver, queued_driver), timeout=2)
        assert (await storage.get_agent_task(running.task_id)).status == AgentTaskStatus.CANCELLED
        assert (await storage.get_agent_task(queued.task_id)).status == AgentTaskStatus.CANCELLED
        assert (await storage.get_activation_permit(running.task_id)).state == "released"
        assert await storage.get_activation_permit(queued.task_id) is None
        assert operations["release_activation_permit"] == 0


async def test_activation_abort_still_releases_before_terminal_settlement(tmp_path, monkeypatch):
    async with _stack(tmp_path) as (storage, runtime, node, operations):
        claim = runtime._claim_activation_permit
        dispatched = []

        async def cancel_after_claim(task):
            await claim(task)
            # Reproduce the cancel flag winning between the durable claim and
            # the guarded RUNNING publication, before any terminal receipt.
            task.cancel_requested = True

        async def handler(run):
            dispatched.append(run)

        runtime._turn_handler = handler
        monkeypatch.setattr(runtime, "_claim_activation_permit", cancel_after_claim)
        handle = await runtime.enqueue(_envelope(node), "cancel at activation")
        assert (await _drain(runtime, handle.task_id)).status == AgentTaskStatus.CANCELLED
        assert dispatched == []
        assert operations["release_activation_permit"] == 1
        assert (await storage.get_activation_permit(handle.task_id)).state == "released"


async def test_receipt_is_unavailable_until_terminal_and_permit_commit(tmp_path, monkeypatch):
    async with _stack(tmp_path) as (storage, _runtime, node, _operations):
        # Open before creating the task; a newly started storage performs
        # recovery and would abandon another connection's pending task.
        observer = await SessionStorage.open(str(tmp_path / "sessions.db"))
        authority = _envelope(node).metadata["activation_authority"]
        await storage.create_agent_task(AgentTaskRecord(
            task_id="receipt", session_key=node.session_key,
            details={"activation_authority": authority},
        ))
        await storage.claim_activation_permit(
            "receipt", session_key=node.session_key, **authority,
        )
        committing, release = asyncio.Event(), asyncio.Event()
        commit = storage._commit_transaction
        settlement = None

        async def gated_commit(conn, operation, deadline, started):
            committing.set()
            await release.wait()
            await commit(conn, operation, deadline, started)

        monkeypatch.setattr(storage, "_commit_transaction", gated_commit)
        try:
            settlement = asyncio.create_task(storage.settle_agent_task_with_receipt(
                "receipt", session_key=node.session_key, details_patch={}, remove_detail_keys=[],
                status=AgentTaskStatus.SUCCEEDED, finished_at=123, terminal_reason="completed",
            ))
            await asyncio.wait_for(committing.wait(), timeout=2)
            assert not settlement.done()
            assert (await observer.get_agent_task("receipt")).status == AgentTaskStatus.QUEUED
            assert (await observer.get_activation_permit("receipt")).state == "active"
            release.set()
            assert await asyncio.wait_for(settlement, timeout=2) == AgentTaskSettlementReceipt(
                task_id="receipt", session_key=node.session_key,
            )
            assert (await observer.get_agent_task("receipt")).status == AgentTaskStatus.SUCCEEDED
            assert (await observer.get_activation_permit("receipt")).state == "released"
        finally:
            release.set()
            if settlement is not None:
                await asyncio.gather(settlement, return_exceptions=True)
            await observer.close()


@pytest.mark.parametrize("committed", [False, True])
async def test_cancelled_commit_cannot_issue_atomic_receipt(
    tmp_path, monkeypatch, committed,
):
    storage = await SessionStorage.open(str(tmp_path / "cancelled-commit.db"))
    key = "agent:main:webchat:cancelled-commit"
    try:
        node = SessionNode(session_key=key, session_id="owner", epoch=0)
        authority = _envelope(node).metadata["activation_authority"]
        await storage.upsert_session(node)
        await storage.create_agent_task(AgentTaskRecord(
            task_id="task", session_key=key, details={"activation_authority": authority},
        ))
        await storage.claim_activation_permit("task", session_key=key, **authority)
        commit = storage._commit_transaction

        async def cancel_after_commit(conn, operation, deadline, started):
            if committed:
                await commit(conn, operation, deadline, started)
            raise asyncio.CancelledError("synthetic cancellation after commit")

        monkeypatch.setattr(storage, "_commit_transaction", cancel_after_commit)
        with pytest.raises(asyncio.CancelledError):
            await storage.settle_agent_task_with_receipt(
                "task", session_key=key, details_patch={}, remove_detail_keys=[],
                status=AgentTaskStatus.CANCELLED, finished_at=123, terminal_reason="cancelled",
            )
        assert (await storage.get_agent_task("task")).status == (
            AgentTaskStatus.CANCELLED if committed else AgentTaskStatus.QUEUED
        )
        assert (await storage.get_activation_permit("task")).state == (
            "released" if committed else "active"
        )
        assert not storage.conn.in_transaction
        assert not storage._operation_lock.locked()
    finally:
        await storage.close()
