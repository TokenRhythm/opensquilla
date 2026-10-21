from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.sandbox import run_context, run_context_service, user_grants
from opensquilla.sandbox.run_context import DomainGrant, RunContext
from opensquilla.sandbox.run_mode import RunMode
from tests.helpers.sqlite_process_probe import run_sqlite_probe


class SessionManager:
    def __init__(self, workspace, *, chat_grant=False):
        context = RunContext(
            run_mode=RunMode.FULL,
            workspace=str(workspace),
            domains=(DomainGrant("example.com", "chat"),) if chat_grant else (),
        )
        self.node = SimpleNamespace(
            session_key="synthetic-session",
            workspace_id=None,
            origin={run_context.RUN_CONTEXT_ORIGIN_KEY: context.to_origin_payload()},
        )
        self.initial_origin = copy.deepcopy(self.node.origin)
        self.updates = []
        self.update_hook = None
        self.updated = asyncio.Event()
        self.loop_thread = threading.get_ident()

    async def get_session(self, _session_key):
        assert threading.get_ident() == self.loop_thread
        return self.node

    async def update(self, _session_key, **fields):
        assert threading.get_ident() == self.loop_thread
        self.updates.append(copy.deepcopy(fields))
        for key, value in fields.items():
            setattr(self.node, key, value)
        if self.update_hook is not None:
            await self.update_hook()
        self.updated.set()
        return self.node


def config():
    return SimpleNamespace(
        sandbox=SimpleNamespace(run_mode="full"),
        permissions=SimpleNamespace(default_mode="full"),
    )


def add_domain(manager, workspace):
    return run_context_service.add_domain_grant(
        manager,
        manager.node.session_key,
        domain="example.com",
        scope="workspace",
        config=config(),
        workspace=str(workspace),
    )


def remove_domain(manager, workspace):
    return run_context_service.remove_domain_grant(
        manager,
        manager.node.session_key,
        domain="example.com",
        scope="workspace",
        config=config(),
        workspace=str(workspace),
    )


def read_context(manager, workspace):
    return run_context.get_run_context(
        manager,
        manager.node.session_key,
        config=config(),
        workspace=str(workspace),
    )


async def started(event):
    assert await asyncio.to_thread(event.wait, 3), "grant operation did not start"


async def cancel_twice(task):
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancelled owner abandoned its grant operation"


@pytest.mark.parametrize("operation", ["add", "remove"])
async def test_service_write_lock_does_not_block_event_loop(tmp_path, monkeypatch, operation):
    manager = SessionManager(tmp_path)
    if operation == "remove":
        user_grants.upsert_domain_grant({"domain": "example.com", "scope": "workspace"})
    else:
        user_grants.load_user_grants_payload()

    original_connect = sqlite3.connect
    write_started = threading.Event()
    transactions = []

    def connect(*args, **kwargs):
        # Bound a synchronous regression's SQLite wait, not production timeouts.
        kwargs["timeout"] = 0.3
        connection = original_connect(*args, **kwargs)

        def trace(statement):
            if statement.lstrip().startswith("BEGIN IMMEDIATE"):
                transactions.append(statement)
                write_started.set()

        connection.set_trace_callback(trace)
        return connection

    with closing(original_connect(user_grants._state_path(), isolation_level=None)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(user_grants.sqlite3, "connect", connect)
        mutate = add_domain if operation == "add" else remove_domain
        task = asyncio.create_task(mutate(manager, tmp_path))
        try:
            await started(write_started)
            assert not task.done()
            assert writer.in_transaction
            writer.rollback()
            await asyncio.wait_for(task, 3)
        finally:
            writer.rollback()
            await asyncio.gather(task, return_exceptions=True)

    assert len(transactions) == 1
    domains = user_grants.load_user_grants_payload()["domains"]
    assert [grant["domain"] for grant in domains] == (["example.com"] if operation == "add" else [])


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("cancel_all", [False, True])
def test_cancelled_post_persist_write_settles_and_rolls_back_on_failure(
    tmp_path, monkeypatch, failure, cancel_all,
):
    # The outer process bounds actual writes and cleanup without a third
    # cancellation from wait_for changing the owner's expected CancelledError.
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    result = run_sqlite_probe(
        tmp_path, _GRANTS_CANCELLATION_PROBE, (str(int(failure)), str(int(cancel_all)), "none"),
    )
    assert result.timed_out is None, result.output
    assert result.returncode == 0, result.output
    assert "probe_stdout_utf8=\u2713" in result.output
    assert "probe_stderr_utf8=\u2713" in result.output
    assert "phase=cleanup-complete" in result.output
    assert "grants_cancellation_contract=passed" in result.output


_GRANTS_CANCELLATION_PROBE = (
    Path(__file__).with_name("fixtures") / "user_grants_cancellation_probe.py"
)


def test_grants_cancellation_probe_preserves_slow_real_commit(tmp_path):
    result = run_sqlite_probe(tmp_path, _GRANTS_CANCELLATION_PROBE, ("0", "0", "slow-commit"))
    assert result.timed_out is None, result.output
    assert result.returncode == 0, result.output
    assert "grant_commit_delay=3.1" in result.output
    assert "phase=cleanup-complete" in result.output
    assert "grants_cancellation_contract=passed" in result.output


@pytest.mark.parametrize(("fault", "failure"), [
    ("duplicate-write", "user grant was written more than once"),
    ("leak-connection", "user-grant database connection was not released"),
])
def test_grants_cancellation_probe_rejects_broken_contracts(tmp_path, fault, failure):
    result = run_sqlite_probe(tmp_path, _GRANTS_CANCELLATION_PROBE, ("0", "0", fault))
    assert result.timed_out is None, result.output
    assert result.returncode != 0, result.output
    assert f"fault={fault}" in result.output.splitlines(), result.output
    assert "phase=cleanup-complete" in result.output
    assert result.output.rstrip().splitlines()[-1] == f"AssertionError: {failure}", result.output
    assert "grants_cancellation_contract=passed" not in result.output


def test_grants_cancellation_probe_bounds_noncooperative_worker(tmp_path):
    result = run_sqlite_probe(
        tmp_path, _GRANTS_CANCELLATION_PROBE, ("0", "0", "hang-commit"),
        ready_signal="GRANTS_COMMIT_HANG_ENTERED", execution_seconds=2,
    )
    assert result.timed_out == "execution/cleanup", result.output
    assert result.returncode != 0, result.output
    assert "fault=hang-commit" in result.output.splitlines(), result.output
    assert "phase=owner-cancellation-verified" in result.output.splitlines(), result.output
    assert "phase=grant-commit-blocked" in result.output.splitlines(), result.output
    assert "GRANTS_COMMIT_HANG_ENTERED" in result.output.splitlines(), result.output
    assert "phase=cleanup-complete" not in result.output
    assert "grants_cancellation_contract=passed" not in result.output


def test_grants_cancellation_probe_preserves_failure_before_commit(tmp_path):
    result = run_sqlite_probe(
        tmp_path, _GRANTS_CANCELLATION_PROBE, ("0", "0", "hang-commit-with-prior-error"),
        ready_signal="GRANTS_COMMIT_HANG_ENTERED", execution_seconds=2,
    )
    assert result.timed_out is None, result.output
    assert result.returncode != 0, result.output
    assert "fault=hang-commit-with-prior-error" in result.output.splitlines(), result.output
    assert "phase=grant-write-held" in result.output.splitlines(), result.output
    assert "phase=grant-commit-entered" in result.output.splitlines(), result.output
    assert "phase=grant-commit-completed" in result.output.splitlines(), result.output
    assert "phase=cleanup-complete" in result.output
    assert result.output.rstrip().splitlines()[-1] == (
        "RuntimeError: prior failure must not become an expected grant commit hang"
    ), result.output
    assert "GRANTS_COMMIT_HANG_ENTERED" not in result.output
    assert "grants_cancellation_contract=passed" not in result.output


async def test_session_persist_failure_does_not_submit_user_store_write(tmp_path, monkeypatch):
    manager = SessionManager(tmp_path)

    async def fail_update(*_args, **_kwargs):
        raise RuntimeError("synthetic session persistence failure")

    def unexpected_write(_payload):
        pytest.fail("failed session persistence must not write a user grant")

    monkeypatch.setattr(manager, "update", fail_update)
    monkeypatch.setattr(user_grants, "upsert_domain_grant", unexpected_write)
    with pytest.raises(RuntimeError, match="synthetic session persistence"):
        await add_domain(manager, tmp_path)
    assert user_grants.load_user_grants_payload()["domains"] == []


async def test_migrating_read_and_workspace_revoke_cannot_restore_revoked_grant(
    tmp_path, monkeypatch,
):
    manager = SessionManager(tmp_path)
    legacy_path = user_grants._legacy_state_path()
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps({"domains": [{"domain": "example.com", "scope": "workspace"}]}),
        encoding="utf-8",
    )
    migration_started = threading.Event()
    removal_started = threading.Event()
    release = threading.Event()
    original_upsert_row = user_grants._upsert_row
    original_remove = user_grants.remove_domain_grant
    first_row = True

    def upsert_row(*args, **kwargs):
        nonlocal first_row
        if first_row:
            first_row = False
            migration_started.set()
            assert release.wait(3)
        return original_upsert_row(*args, **kwargs)

    def remove(domain):
        removal_started.set()
        return original_remove(domain)

    monkeypatch.setattr(user_grants, "_upsert_row", upsert_row)
    monkeypatch.setattr(user_grants, "remove_domain_grant", remove)
    read_task = asyncio.create_task(read_context(manager, tmp_path))
    remove_task = None
    try:
        await started(migration_started)
        remove_task = asyncio.create_task(remove_domain(manager, tmp_path))
        # The service's include_user_grants=False path must still join the store queue.
        for _ in range(3):
            await asyncio.sleep(0)
        assert not remove_task.done()
        assert not removal_started.is_set()
        release.set()
        await asyncio.wait_for(asyncio.gather(read_task, remove_task), 3)
    finally:
        release.set()
        await asyncio.gather(
            read_task, *([remove_task] if remove_task is not None else []),
            return_exceptions=True,
        )

    assert removal_started.is_set()
    assert user_grants.load_user_grants_payload()["domains"] == []
    assert not legacy_path.exists()


@pytest.mark.parametrize("cancel_all", [False, True])
async def test_cancelled_queued_mutation_does_not_persist_session_or_user_grant(
    tmp_path, monkeypatch, cancel_all,
):
    manager = SessionManager(tmp_path, chat_grant=True)
    read_started = threading.Event()
    release = threading.Event()
    original_load = run_context.load_user_grants_payload
    block_next_read = True
    writes = []

    def load():
        nonlocal block_next_read
        if block_next_read:
            block_next_read = False
            read_started.set()
            assert release.wait(3)
        return original_load()

    def upsert(payload):
        writes.append(payload)

    monkeypatch.setattr(run_context, "load_user_grants_payload", load)
    monkeypatch.setattr(user_grants, "upsert_domain_grant", upsert)
    existing_tasks = asyncio.all_tasks()
    read_task = asyncio.create_task(read_context(manager, tmp_path))
    task = None
    try:
        await started(read_started)
        task = asyncio.create_task(add_domain(manager, tmp_path))
        for _ in range(3):
            await asyncio.sleep(0)
        assert not task.done()
        assert manager.updates == []
        if cancel_all:
            for pending in asyncio.all_tasks() - existing_tasks:
                pending.cancel()
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert manager.node.origin == manager.initial_origin
        assert manager.updates == []
        assert writes == []
    finally:
        release.set()
        await asyncio.gather(
            read_task, *([task] if task is not None else []),
            return_exceptions=True,
        )

    assert writes == []
    assert user_grants.load_user_grants_payload()["domains"] == []
