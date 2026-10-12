"""Failed approval persistence must not poison the next request's connection."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest

from opensquilla.application.approval_queue import ApprovalQueue
from opensquilla.application.approval_rpc import approval_request_rpc_payload_async


class FaultConnection:
    """Inject one fault after real SQLite changes, preserving transaction behavior."""

    def __init__(
        self, connection, *, fault="statement", rollback="normal",
        error_type=sqlite3.OperationalError, close_failures=0,
    ):
        self.connection = connection
        self.fault = fault
        self.rollback_behavior = rollback
        self.error_type = error_type
        self.close_failures = close_failures
        self.armed = True
        self.closed = False
        self.rollback_entered = threading.Event()
        self.rollback_release = threading.Event()

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args):
        result = self.connection.execute(sql, *args)
        if self.armed and self.fault == "statement" and sql.startswith(("INSERT", "UPDATE")):
            self.armed = False
            raise self.error_type("injected statement failure")
        return result

    def commit(self):
        if self.armed and self.fault == "commit":
            self.armed = False
            raise sqlite3.OperationalError("injected commit failure")
        result = self.connection.commit()
        if self.armed and self.fault == "post_commit":
            self.armed = False
            raise sqlite3.OperationalError("injected post-commit failure")
        return result

    def rollback(self):
        self.rollback_entered.set()
        if self.rollback_behavior == "raise":
            raise sqlite3.OperationalError("injected rollback failure")
        if self.rollback_behavior == "noop":
            return None
        if self.rollback_behavior == "blocked":
            assert self.rollback_release.wait(5), "test did not release rollback"
        return self.connection.rollback()

    def close(self):
        if self.close_failures:
            self.close_failures -= 1
            raise sqlite3.OperationalError("injected close failure")
        self.connection.close()
        self.closed = True


def snapshot(queue):
    return {
        table: [tuple(row) for row in queue._conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in ("approval_queue", "channel_approval_codes")
    }


def pending_snapshot(queue):
    return {
        aid: (
            entry.namespace, json.dumps(entry.params, sort_keys=True), entry.created_at,
            entry.resolved, entry.approved, entry.consumed, entry.deadline, entry.resolution,
            entry.claim_token, entry.claim_started_at, entry._event.is_set(),
        )
        for aid, entry in queue._pending.items()
    }


async def finish_worker(coroutine):
    task = asyncio.create_task(coroutine)
    done, _ = await asyncio.wait({task}, timeout=5)
    if task not in done:
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=5)
        assert task in done, "approval worker did not settle after cancellation"
        if not task.cancelled():
            task.exception()
        pytest.fail("approval worker exceeded the test deadline")
    return task.result()


def assert_independent_writer_available(path):
    with closing(sqlite3.connect(path, timeout=0)) as other:
        other.execute("BEGIN IMMEDIATE")
        other.rollback()


def operation_for(queue, operation):
    aid = queue.request(params={"sessionKey": "isolated"})
    if operation in {"consume", "reopen"}:
        queue.resolve(aid, True)
    claim = None
    if operation in {"expire_claimed", "finalize", "complete", "release"}:
        claim = queue.claim_resolution(aid)
        if operation == "complete":
            queue.finalize_claimed_resolution(aid, claim, True)
    if operation == "stale_claim":
        queue._conn.execute(
            "UPDATE approval_queue SET claim_token='expired', claim_started_at=0 "
            "WHERE approval_id=?", (aid,),
        )
    operations = {
        "request": lambda: queue.request(params={"command": "safe"}),
        "bind_channel": lambda: queue.bind_channel_code(
            "code", approval_id=aid, namespace="exec", session_key="isolated",
            owner_sender_id="sender",
        ),
        "stale_claim": lambda: queue.get(aid),
        "update_params": lambda: queue.update_params(aid, {"command": "safe"}),
        "rearm": lambda: queue._rearm_deadline(aid, 2_000_000_000),
        "extend": lambda: queue.extend(aid, 30),
        "expire": lambda: queue.expire_pending(aid),
        "expire_claimed": lambda: queue.expire_claimed_resolution(aid, claim),
        "resolve": lambda: queue.resolve(aid, True),
        "claim": lambda: queue.claim_resolution(aid),
        "finalize": lambda: queue.finalize_claimed_resolution(aid, claim, True),
        "complete": lambda: queue.complete_claimed_resolution(aid, claim),
        "release": lambda: queue.release_resolution_claim(aid, claim),
        "reopen": lambda: queue.reopen_resolved_approval(aid),
        "consume": lambda: queue.consume(aid),
    }
    return operations[operation]


async def test_surrogate_json_request_does_not_poison_following_approval(tmp_path):
    path = tmp_path / "approvals.sqlite"
    queue = ApprovalQueue(db_path=str(path))
    events = []
    queue.add_event_listener(lambda *args: events.append(args))
    payload = json.loads(
        r'{"toolName":"exec_command","args":{"command":"\ud800"},"sessionKey":"isolated"}',
    )
    try:
        with pytest.raises(UnicodeEncodeError):
            await finish_worker(
                approval_request_rpc_payload_async(queue, namespace="exec", params=payload),
            )
        assert not queue._conn.in_transaction
        assert queue._pending == {}
        assert events == []
        assert_independent_writer_available(path)
        created = await finish_worker(queue.request_async(params={"command": "safe"}))
        await finish_worker(queue.resolve_async(created, True))
        await finish_worker(queue.consume_async(created))
        assert (await finish_worker(queue.get_async(created))).consumed
    finally:
        queue.close()


@pytest.mark.parametrize("fault", ["statement", "commit"])
@pytest.mark.parametrize("operation", [
    "request", "bind_channel", "stale_claim", "update_params", "rearm", "extend",
    "expire", "expire_claimed", "resolve", "claim", "finalize", "complete", "release",
    "reopen", "consume",
])
def test_interrupted_mutation_rolls_back_before_next_operation(tmp_path, operation, fault):
    path = tmp_path / "approvals.sqlite"
    queue = ApprovalQueue(db_path=str(path))
    mutate = operation_for(queue, operation)
    before = snapshot(queue)
    pending_before = pending_snapshot(queue)
    events = []
    queue.add_event_listener(lambda *args: events.append(args))
    connection = FaultConnection(queue._conn, fault=fault)
    queue._conn = connection
    try:
        with pytest.raises(sqlite3.OperationalError, match=f"injected {fault} failure"):
            mutate()
        assert not connection.in_transaction
        assert snapshot(queue) == before
        assert pending_snapshot(queue) == pending_before
        assert events == []
        assert_independent_writer_available(path)
        created = queue.request(params={"command": "recovery"})
        queue.resolve(created, False)
        assert queue.get(created).resolved
    finally:
        queue.close()


@pytest.mark.parametrize("rollback", ["raise", "noop"])
def test_failed_rollback_closes_and_quarantines_queue(tmp_path, rollback):
    path = tmp_path / "approvals.sqlite"
    queue = ApprovalQueue(db_path=str(path))
    aid = queue.request(params={})
    connection = FaultConnection(queue._conn, fault="commit", rollback=rollback)
    queue._conn = connection
    events = []
    queue.add_event_listener(lambda *args: events.append(args))
    try:
        with pytest.raises(RuntimeError, match="Approval queue storage is unavailable"):
            queue.resolve(aid, True)
        assert connection.closed
        assert events == []
        for operation in (lambda: queue.request(), lambda: queue.get(aid), queue.list_pending):
            with pytest.raises(RuntimeError, match="approval.*unavailable|Approval.*unavailable"):
                operation()
        assert_independent_writer_available(path)
        with closing(sqlite3.connect(path)) as other:
            assert other.execute(
                "SELECT resolved, approved FROM approval_queue WHERE approval_id=?", (aid,),
            ).fetchone() == (0, 0)
        assert queue.get_settings().mode == "prompt"
    finally:
        queue.close()


def test_failed_close_still_quarantines_and_explicit_close_can_retry(tmp_path):
    path = tmp_path / "approvals.sqlite"
    queue = ApprovalQueue(db_path=str(path))
    aid = queue.request()
    connection = FaultConnection(
        queue._conn, fault="commit", rollback="raise", close_failures=1,
    )
    queue._conn = connection
    pending_before = pending_snapshot(queue)
    events = []
    queue.add_event_listener(lambda *args: events.append(args))
    try:
        with pytest.raises(RuntimeError, match="Approval queue storage is unavailable"):
            queue.resolve(aid, True)
        assert not connection.closed
        assert connection.in_transaction
        assert pending_snapshot(queue) == pending_before
        assert events == []
        for operation in (lambda: queue.request(), lambda: queue.get(aid), queue.list_pending):
            with pytest.raises(RuntimeError, match="Approval queue storage is unavailable"):
                operation()
        queue.close()
        assert connection.closed
        assert_independent_writer_available(path)
        with closing(sqlite3.connect(path)) as other:
            assert other.execute(
                "SELECT resolved, approved FROM approval_queue WHERE approval_id=?", (aid,),
            ).fetchone() == (0, 0)
    finally:
        queue.close()


async def test_repeated_cancellation_waits_for_failed_nested_operation_rollback(tmp_path):
    path = tmp_path / "approvals.sqlite"
    queue = ApprovalQueue(db_path=str(path))
    aid = queue.request()
    connection = FaultConnection(queue._conn, fault="commit", rollback="blocked")
    queue._conn = connection
    task = asyncio.create_task(queue.run_operation_async(lambda: queue.resolve(aid, True)))
    try:
        assert await asyncio.to_thread(connection.rollback_entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert queue._async_lock.locked()
        connection.rollback_release.set()
        done, _ = await asyncio.wait({task}, timeout=5)
        assert task in done, "worker did not finish its rollback"
        with pytest.raises(asyncio.CancelledError):
            task.result()
        assert not queue._async_lock.locked()
        assert not connection.in_transaction
        assert_independent_writer_available(path)
        assert not (await finish_worker(queue.get_async(aid))).resolved
        await finish_worker(queue.resolve_async(aid, False))
    finally:
        connection.rollback_release.set()
        done, _ = await asyncio.wait({task}, timeout=5)
        if task in done:
            if not task.cancelled():
                task.exception()
            queue.close()
        assert task in done, "worker did not release the queue for cleanup"


def test_begin_contention_does_not_poison_connection(tmp_path):
    path = tmp_path / "approvals.sqlite"
    queue = ApprovalQueue(db_path=str(path))
    queue._conn.execute("PRAGMA busy_timeout=0")
    try:
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                queue.request()
            assert not queue._conn.in_transaction
            writer.rollback()
        assert queue.get(queue.request()).resolved is False
    finally:
        queue.close()


def test_nested_transaction_rejection_does_not_rollback_outer_owner(tmp_path):
    path = tmp_path / "approvals.sqlite"
    queue = ApprovalQueue(db_path=str(path))
    aid = queue.request()
    try:
        with queue._transaction():
            queue._conn.execute(
                "UPDATE approval_queue SET deadline=123 WHERE approval_id=?", (aid,),
            )
            with pytest.raises(RuntimeError, match="transaction is already active"):
                queue.request()
            assert queue._conn.in_transaction
            with closing(sqlite3.connect(path)) as reader:
                assert reader.execute(
                    "SELECT deadline FROM approval_queue WHERE approval_id=?", (aid,),
                ).fetchone() == (0.0,)
        assert queue.get(aid).deadline == 123
    finally:
        queue.close()


def test_uuid_collision_retries_only_after_rollback(tmp_path, monkeypatch):
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    existing = queue.request()
    ids = iter((existing, "012345abcdef"))
    monkeypatch.setattr(
        "opensquilla.application.approval_queue.uuid.uuid4",
        lambda: SimpleNamespace(hex=next(ids)),
    )
    events = []
    queue.add_event_listener(lambda *args: events.append(args))
    try:
        fresh = queue.request()
        assert fresh == "012345abcdef"
        assert len(events) == 1
        assert not queue._conn.in_transaction
        assert {row["id"] for row in queue.list_pending()} == {existing, fresh}
    finally:
        queue.close()


def test_channel_code_collision_remains_false_and_connection_recovers(tmp_path):
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    first, second = queue.request(), queue.request()
    params = {"namespace": "exec", "session_key": "isolated", "owner_sender_id": "sender"}
    try:
        assert queue.bind_channel_code("taken", approval_id=first, **params)
        assert not queue.bind_channel_code("taken", approval_id=second, **params)
        assert not queue._conn.in_transaction
        assert queue.bind_channel_code("fresh", approval_id=second, **params)
    finally:
        queue.close()


def test_integrity_error_with_failed_rollback_cannot_enter_collision_retry(tmp_path):
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    connection = FaultConnection(
        queue._conn, rollback="raise", error_type=sqlite3.IntegrityError,
    )
    queue._conn = connection
    try:
        with pytest.raises(RuntimeError, match="Approval queue storage is unavailable"):
            queue.request()
        assert connection.closed
        assert queue._pending == {}
    finally:
        queue.close()


@pytest.mark.parametrize("rollback", ["raise", "noop"])
def test_noop_resolution_rollback_failure_is_also_quarantined(tmp_path, rollback):
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    aid = queue.request()
    queue.resolve(aid, True)
    connection = FaultConnection(queue._conn, fault="none", rollback=rollback)
    queue._conn = connection
    try:
        with pytest.raises(RuntimeError, match="Approval queue storage is unavailable"):
            queue.resolve(aid, True)
        assert connection.closed
    finally:
        queue.close()


def test_real_sql_abort_rolls_back_and_next_resolution_succeeds(tmp_path):
    path = tmp_path / "approvals.sqlite"
    queue = ApprovalQueue(db_path=str(path))
    aid = queue.request()
    queue._conn.execute(
        "CREATE TEMP TRIGGER reject_decision BEFORE UPDATE ON approval_queue "
        "BEGIN SELECT RAISE(ABORT, 'reject injected decision'); END",
    )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="reject injected decision"):
            queue.resolve(aid, True)
        assert not queue._conn.in_transaction
        assert_independent_writer_available(path)
        queue._conn.execute("DROP TRIGGER reject_decision")
        queue.resolve(aid, True)
        assert queue.get(aid).approved
    finally:
        queue.close()


def test_commit_completion_followed_by_error_preserves_idempotent_resolution(tmp_path):
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    aid = queue.request()
    events = []
    queue.add_event_listener(lambda *args: events.append(args))
    queue._conn = FaultConnection(queue._conn, fault="post_commit")
    try:
        with pytest.raises(sqlite3.OperationalError, match="post-commit failure"):
            queue.resolve(aid, True)
        assert not queue._conn.in_transaction
        assert not queue._pending[aid].resolved
        assert events == []
        queue.resolve(aid, True)
        assert queue.get(aid).approved
        queue.consume(aid)
        with pytest.raises(ValueError, match="already consumed"):
            queue.consume(aid)
    finally:
        queue.close()


def test_worker_cancelled_exception_after_write_rolls_back(tmp_path):
    path = tmp_path / "approvals.sqlite"
    queue = ApprovalQueue(db_path=str(path))
    aid = queue.request()
    queue._conn = FaultConnection(queue._conn, error_type=asyncio.CancelledError)
    try:
        with pytest.raises(asyncio.CancelledError, match="statement failure"):
            queue.resolve(aid, True)
        assert not queue._conn.in_transaction
        assert not queue.get(aid).resolved
        assert_independent_writer_available(path)
    finally:
        queue.close()


def test_claim_completion_notifies_only_after_committed_completion(tmp_path):
    path = tmp_path / "approvals.sqlite"
    queue = ApprovalQueue(db_path=str(path))
    aid = queue.request()
    claim = queue.claim_resolution(aid)
    events = []

    def notified(event, payload):
        assert not queue._conn.in_transaction
        with closing(sqlite3.connect(path)) as reader:
            assert reader.execute(
                "SELECT resolved, approved, claim_token FROM approval_queue WHERE approval_id=?",
                (aid,),
            ).fetchone() == (1, 1, None)
        events.append((event, payload["id"]))

    queue.add_event_listener(notified)
    try:
        queue.finalize_claimed_resolution(aid, claim, True)
        assert not queue.get(aid)._event.is_set()
        assert not queue.status(aid)["resolved"]
        assert events == []
        queue.complete_claimed_resolution(aid, claim)
        assert queue.get(aid)._event.is_set()
        assert events == [("resolved", aid)]
        assert queue.status(aid)["approved"]
    finally:
        queue.close()
