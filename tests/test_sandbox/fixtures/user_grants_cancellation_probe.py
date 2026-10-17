"""Observe real grant settlement without a timeout cancelling its owner again."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from opensquilla.sandbox import user_grants
from tests.helpers.sqlite_process_probe import READY
from tests.test_sandbox.test_user_grants_service_workers import (
    SessionManager,
    add_domain,
    cancel_twice,
)


async def observe_boundary(task, event):
    observation = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait([task, observation], return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task  # Surface the actual early failure rather than a readiness timeout.
            raise AssertionError("grant owner exited before its expected boundary")
        await observation
    finally:
        observation.cancel()
        await asyncio.gather(observation, return_exceptions=True)


async def scenario(directory: Path, failure: bool, cancel_all: bool, fault: str) -> None:
    # Match the original sandbox autouse fixture. Importing its module does not
    # run that fixture, and the probe must never use a real user's grant store.
    os.environ["OPENSQUILLA_STATE_DIR"] = str(directory / "opensquilla-home")
    loop = asyncio.get_running_loop()
    # This isolated scenario owns one grant operation. Keep its SQLite handles
    # and deliberate leak cleanup on that same owned executor thread.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="grants-probe")
    loop.set_default_executor(executor)
    manager = SessionManager(directory, chat_grant=True)
    write_started = asyncio.Event()
    release = threading.Event()
    cancellation_checked = threading.Event()
    rollback_started = asyncio.Event()
    rollback_release = asyncio.Event()
    calls = []
    connections = []
    commits = 0
    original_upsert = user_grants.upsert_domain_grant
    original_connect = sqlite3.connect
    task = None

    class ObservedConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.owner_thread = threading.get_ident()
            self.write_connection = False
            self.closed = False
            connections.append(self)

        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if sql.strip().upper() == "BEGIN IMMEDIATE":
                self.write_connection = True
            return result

        def commit(self):
            nonlocal commits
            if self.write_connection:
                assert threading.get_ident() != manager.loop_thread
                assert self.in_transaction
                print("phase=grant-commit-entered", flush=True)
                if (
                    fault in {"hang-commit", "hang-commit-with-prior-error"}
                    and cancellation_checked.is_set()
                ):
                    print("fault=hang-commit", flush=True)
                    print("phase=grant-commit-blocked", flush=True)
                    print("GRANTS_COMMIT_HANG_ENTERED", flush=True)
                    threading.Event().wait()  # Actual SQL worker; the parent owns its watchdog.
                started = time.monotonic()
                if fault == "slow-commit":
                    print("grant_commit_delay=3.1", flush=True)
                    time.sleep(3.1)  # Inject commit latency, not a readiness heuristic.
                result = super().commit()
                commits += 1
                print("phase=grant-commit-completed", flush=True)
                if fault == "slow-commit":
                    elapsed = time.monotonic() - started
                    print(f"grant_commit_elapsed_seconds={elapsed:.6f}", flush=True)
                    assert elapsed > 3, "slow commit did not exceed the old three-second guard"
                return result
            return super().commit()

        def close(self):
            if fault == "leak-connection" and self.write_connection and not self.closed:
                print("fault=leak-connection", flush=True)
                return  # Deliberately retain a real native connection until cleanup.
            super().close()
            self.closed = True

    def connect(*args, **kwargs):
        kwargs["factory"] = ObservedConnection
        return original_connect(*args, **kwargs)

    async def pause_rollback():
        if len(manager.updates) == 2:
            rollback_started.set()
            await rollback_release.wait()

    def upsert(payload):
        assert threading.get_ident() != manager.loop_thread
        calls.append("write")
        # The normal get_run_context read has already opened the real store;
        # this is the held mutation boundary, not a separate prewarming step.
        if fault != "hang-commit":
            print(READY, flush=True)
        print("phase=grant-write-held", flush=True)
        loop.call_soon_threadsafe(write_started.set)
        release.wait()
        if failure:
            raise sqlite3.OperationalError("synthetic grant write failure")
        original_upsert(payload)
        if fault == "duplicate-write":
            print("fault=duplicate-write", flush=True)
            original_upsert(payload)

    def release_worker_connections():
        for connection in connections:
            if not connection.closed:
                assert connection.owner_thread == threading.get_ident()
                sqlite3.Connection.close(connection)
                connection.closed = True

    manager.update_hook = pause_rollback
    with (
        patch.object(user_grants.sqlite3, "connect", connect),
        patch.object(user_grants, "upsert_domain_grant", upsert),
    ):
        try:
            existing_tasks = asyncio.all_tasks()
            task = asyncio.create_task(add_domain(manager, directory))
            await observe_boundary(task, write_started)
            assert len(manager.updates) == 1
            assert manager.node.origin != manager.initial_origin
            if cancel_all:
                for _ in range(2):
                    for pending in asyncio.all_tasks() - existing_tasks:
                        pending.cancel()
                    await asyncio.sleep(0)
                    assert not task.done(), "cancelled owner abandoned its grant operation"
            else:
                await cancel_twice(task)
            if fault == "hang-commit-with-prior-error":
                print(f"fault={fault}", flush=True)
                raise RuntimeError("prior failure must not become an expected grant commit hang")
            cancellation_checked.set()
            print("phase=owner-cancellation-verified", flush=True)
            release.set()
            if failure:
                await observe_boundary(task, rollback_started)
                await cancel_twice(task)
                rollback_release.set()
            with pytest.raises(asyncio.CancelledError) as error:
                await task  # No wait_for may inject another cancellation here.
            if failure:
                assert isinstance(error.value.__cause__, sqlite3.OperationalError)
                assert manager.node.origin == manager.initial_origin
                assert len(manager.updates) == 2
            else:
                assert len(manager.updates) == 1
            assert calls == ["write"]
            assert commits == (0 if failure else 1), "user grant was written more than once"
            domains = user_grants.load_user_grants_payload()["domains"]
            assert [grant["domain"] for grant in domains] == ([] if failure else ["example.com"])
            assert all(connection.closed for connection in connections), (
                "user-grant database connection was not released"
            )
        finally:
            original_error = sys.exc_info()[1]
            cleanup_errors = []
            release.set()
            rollback_release.set()
            try:
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
                await loop.run_in_executor(executor, release_worker_connections)
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                executor.shutdown(wait=True)
                assert all(not thread.is_alive() for thread in executor._threads)
                assert all(connection.closed for connection in connections)
            except BaseException as error:
                cleanup_errors.append(error)
            if cleanup_errors:
                primary = original_error if original_error is not None else cleanup_errors[0]
                for error in cleanup_errors:
                    if error is not primary:
                        primary.add_note(f"cleanup also failed: {error!r}")
                if original_error is None:
                    raise primary
            else:
                print("phase=cleanup-complete", flush=True)
    print("grants_cancellation_contract=passed", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    print("probe_stdout_utf8=\u2713", flush=True)
    print("probe_stderr_utf8=\u2713", file=sys.stderr, flush=True)
    asyncio.run(scenario(
        Path(sys.argv[1]), bool(int(sys.argv[2])), bool(int(sys.argv[3])), sys.argv[4],
    ))
