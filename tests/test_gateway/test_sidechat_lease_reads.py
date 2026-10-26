from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from opensquilla.gateway import sidechat_leases as leases
from opensquilla.session.storage import StorageBusyError


def _storage():
    metadata = {
        "state": "ready", "parentKey": "parent", "parentSessionId": "parent-id",
        "parentEpoch": 0, "leaseExpiresAt": 1,
    }
    child = SimpleNamespace(origin={"sidechat": metadata})
    parent = SimpleNamespace(session_id="parent-id", epoch=0)
    return SimpleNamespace(
        get_session=AsyncMock(side_effect=lambda key: child if key == "child" else parent),
    ), metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_key", ["child", "parent"])
@pytest.mark.parametrize("wrapped", [False, True])
async def test_busy_reads_recover_without_renewing_lease(monkeypatch, failed_key, wrapped):
    storage, metadata = _storage()
    original = storage.get_session.side_effect
    failures = 7

    async def read(key):
        nonlocal failures
        if key == failed_key and failures:
            failures -= 1
            if wrapped:
                raise StorageBusyError("get_session", waited_ms=1, retry_after_ms=1)
            raise sqlite3.OperationalError("database is locked")
        return original(key)

    storage.get_session.side_effect = read
    sleep = AsyncMock()
    monkeypatch.setattr(leases.asyncio, "sleep", sleep)
    log = Mock()
    monkeypatch.setattr(leases, "log", log)
    monkeypatch.setattr(leases.time, "monotonic", lambda: 10.0)
    close = AsyncMock()
    leases.watch_sidechat_lease(storage, "child", close)
    task = leases._leases[(id(storage), "child")]
    await task
    assert [call.args[0] for call in sleep.await_args_list] == [.25, .5, 1, 2, 4, 5, 5]
    assert log.warning.call_count == 1
    assert metadata["leaseExpiresAt"] == 1
    close.assert_awaited_once()
    assert (id(storage), "child") not in leases._leases


@pytest.mark.asyncio
async def test_lease_renewed_during_backoff_is_reread(monkeypatch):
    storage, metadata = _storage()
    original = storage.get_session.side_effect
    storage.get_session.side_effect = [
        sqlite3.OperationalError("database table is locked"),
        original("child"), original("parent"), None,
    ]
    waits = []

    async def sleep(delay):
        waits.append(delay)
        metadata["leaseExpiresAt"] = 40_000

    monkeypatch.setattr(leases.asyncio, "sleep", sleep)
    monkeypatch.setattr(leases.time, "time", lambda: 10.0)
    close = AsyncMock()
    leases.watch_sidechat_lease(storage, "child", close)
    await leases._leases[(id(storage), "child")]
    assert waits == [.25, 30]
    close.assert_not_awaited()
    assert metadata["leaseExpiresAt"] == 40_000


@pytest.mark.asyncio
async def test_cancellation_during_busy_backoff_unregisters_watcher(monkeypatch):
    storage, _ = _storage()
    storage.get_session.side_effect = sqlite3.OperationalError("database is locked")
    waiting = asyncio.Event()

    async def sleep(_):
        waiting.set()
        await asyncio.Future()

    monkeypatch.setattr(leases.asyncio, "sleep", sleep)
    close = AsyncMock()
    leases.watch_sidechat_lease(storage, "child", close)
    task = leases._leases[(id(storage), "child")]
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (id(storage), "child") not in leases._leases
    close.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_busy_read_failure_is_logged_once_and_consumed(monkeypatch):
    storage, _ = _storage()
    storage.get_session.side_effect = sqlite3.OperationalError("no such table: sessions")
    log = Mock()
    monkeypatch.setattr(leases, "log", log)
    close = AsyncMock()
    leases.watch_sidechat_lease(storage, "child", close)
    await leases._leases[(id(storage), "child")]
    assert storage.get_session.await_count == 1
    log.exception.assert_called_once_with("sidechat.lease_watch_failed", session_key="child")
    close.assert_not_awaited()
    assert (id(storage), "child") not in leases._leases
