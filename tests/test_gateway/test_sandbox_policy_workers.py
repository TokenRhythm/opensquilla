from __future__ import annotations

import asyncio
import contextvars
import sqlite3
import threading
from contextlib import closing

import pytest

from opensquilla.application.sandbox_runtime import SandboxPolicyConflictError
from opensquilla.gateway.adapters import sandbox_runtime as adapters
from opensquilla.sandbox.policy_store import SandboxPolicyStore


async def _started(event):
    assert await asyncio.to_thread(event.wait, 3), "policy worker did not start"


def _writer(path):
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("BEGIN IMMEDIATE")
    return connection


def _observe_transactions(monkeypatch, started):
    statements = []

    class ObservedStore(SandboxPolicyStore):
        def _connect(self):
            # Bound a regression's synchronous lock wait, not production policy.
            connection = sqlite3.connect(self._path, timeout=0.3, isolation_level=None)
            connection.row_factory = sqlite3.Row

            def trace(sql):
                if sql.lstrip().startswith("BEGIN IMMEDIATE"):
                    statements.append(sql)
                    started.set()

            connection.set_trace_callback(trace)
            return connection

    monkeypatch.setattr(adapters, "SandboxPolicyStore", ObservedStore)
    return statements


@pytest.mark.parametrize("operation", ["read", "replace"])
async def test_constructor_and_policy_operation_run_off_loop_with_context(
    tmp_path, monkeypatch, operation,
):
    adapter = adapters.GatewaySandboxPolicyAdapter(tmp_path)
    document = await adapter.read_policy()
    loop_thread = threading.get_ident()
    marker = contextvars.ContextVar("policy-worker-context", default=None)
    seen = []

    class ObservedStore(SandboxPolicyStore):
        def __init__(self, path):
            seen.append(("constructor", threading.get_ident(), marker.get()))
            super().__init__(path)

        def read(self):
            seen.append(("read", threading.get_ident(), marker.get()))
            return super().read()

        def compare_and_swap(self, *args):
            seen.append(("replace", threading.get_ident(), marker.get()))
            return super().compare_and_swap(*args)

    monkeypatch.setattr(adapters, "SandboxPolicyStore", ObservedStore)
    token = marker.set("request-scope")
    try:
        if operation == "read":
            await adapter.read_policy()
        else:
            await adapter.replace_policy(document.policy_version, document)
    finally:
        marker.reset(token)
    assert [entry[0] for entry in seen] == ["constructor", operation]
    assert all(entry[1] != loop_thread and entry[2] == "request-scope" for entry in seen)


async def test_policy_write_lock_does_not_stop_loop_progress(tmp_path, monkeypatch):
    adapter = adapters.GatewaySandboxPolicyAdapter(tmp_path)
    document = await adapter.read_policy()
    started = threading.Event()
    transactions = _observe_transactions(monkeypatch, started)
    with closing(_writer(tmp_path / "sessions.db")) as writer:
        task = asyncio.create_task(adapter.replace_policy(document.policy_version, document))
        try:
            await _started(started)
            # Only the event-loop thread releases this real SQLite writer lock.
            # A synchronous implementation times out before it can get here.
            assert not task.done()
            assert writer.in_transaction
            writer.rollback()
            saved = await asyncio.wait_for(task, 3)
        finally:
            writer.rollback()
            await asyncio.gather(task, return_exceptions=True)
    assert saved.policy_version == document.policy_version + 1
    assert len(transactions) == 1


async def test_existing_policy_read_remains_available_under_wal_writer(tmp_path):
    adapter = adapters.GatewaySandboxPolicyAdapter(tmp_path)
    document = await adapter.read_policy()
    with closing(_writer(tmp_path / "sessions.db")) as writer:
        try:
            assert await asyncio.wait_for(adapter.read_policy(), 3) == document
            assert writer.in_transaction
        finally:
            writer.rollback()


async def test_repeated_cancellation_settles_one_physical_policy_write(tmp_path, monkeypatch):
    adapter = adapters.GatewaySandboxPolicyAdapter(tmp_path)
    document = await adapter.read_policy()
    started = threading.Event()
    transactions = _observe_transactions(monkeypatch, started)
    with closing(_writer(tmp_path / "sessions.db")) as writer:
        task = asyncio.create_task(adapter.replace_policy(document.policy_version, document))
        try:
            await _started(started)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            writer.rollback()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        finally:
            writer.rollback()
            await asyncio.gather(task, return_exceptions=True)
    assert len(transactions) == 1
    assert (await adapter.read_policy()).policy_version == document.policy_version + 1


async def test_concurrent_policy_cas_preserves_version_conflict(tmp_path):
    adapter = adapters.GatewaySandboxPolicyAdapter(tmp_path)
    document = await adapter.read_policy()
    results = await asyncio.gather(
        adapter.replace_policy(document.policy_version, document),
        adapter.replace_policy(document.policy_version, document),
        return_exceptions=True,
    )
    conflicts = [result for result in results if isinstance(result, SandboxPolicyConflictError)]
    assert len(conflicts) == 1
    assert conflicts[0].current_policy.policy_version == 1
    saved = [result for result in results if not isinstance(result, BaseException)]
    assert len(saved) == 1 and saved[0].policy_version == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_policy_worker_failure_is_settled_without_retry(tmp_path, monkeypatch, cancel):
    adapter = adapters.GatewaySandboxPolicyAdapter(tmp_path)
    document = await adapter.read_policy()
    started = threading.Event()
    release = threading.Event()
    calls = []

    class BrokenStore(SandboxPolicyStore):
        def compare_and_swap(self, *args):
            calls.append("write")
            started.set()
            assert release.wait(3)
            raise sqlite3.OperationalError("synthetic policy write failure")

    monkeypatch.setattr(adapters, "SandboxPolicyStore", BrokenStore)
    task = asyncio.create_task(adapter.replace_policy(document.policy_version, document))
    try:
        await _started(started)
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError) as raised:
                await asyncio.wait_for(task, 3)
            assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
        else:
            with pytest.raises(sqlite3.OperationalError, match="synthetic policy"):
                await asyncio.wait_for(task, 3)
        assert calls == ["write"]
        assert (await adapter.read_policy()).policy_version == document.policy_version
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
