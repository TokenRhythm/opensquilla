"""Directory title failures preserve the existing retryable RPC contract."""

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway import rpc_sessions  # noqa: F401 - register session handlers
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.session.manager import SessionManager
from opensquilla.session.recovery_reads import RecoveryReadPool, recovery_read_scope
from opensquilla.session.storage import SessionStorage, StorageBusyError


@pytest.fixture
async def manager(tmp_path: Path) -> AsyncIterator[SessionManager]:
    storage = await SessionStorage.open(str(tmp_path / "title-failures.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        for index in range(3):
            key = f"agent:main:webchat:title-failure-{index}"
            await manager.create(key, display_name="WebChat")
            await manager.append_message(key, "user", f"Synthetic title {index}")
        yield manager
    finally:
        await storage.close()


def context(manager: SessionManager) -> RpcContext:
    result = RpcContext(
        conn_id="title-failures",
        config=GatewayConfig(),
        principal=Principal(
            role="operator", scopes=frozenset({"operator.admin"}),
            is_owner=True, authenticated=True,
        ),
    )
    result.session_manager = manager
    return result


@pytest.mark.parametrize("path", ["active", "canonical", "legacy"])
@pytest.mark.parametrize("failure", ["busy", "timeout", "unexpected"])
async def test_title_failure_cannot_become_successful_empty_titles(
    manager: SessionManager, monkeypatch: pytest.MonkeyPatch, path: str, failure: str,
) -> None:
    storage = manager._storage
    if path == "canonical":
        for node in await storage.list_sessions():
            await manager.update(
                node.session_key, derived_title="I cannot assist with that request"
            )
    operation = {
        "active": "list_user_transcript_content_batch",
        "canonical": "list_canonical_user_transcript_content_batch",
        "legacy": "get_transcript",
    }[path]
    error = (
        StorageBusyError(operation, waited_ms=17, retry_after_ms=321,
                         stage="permit", resource="session_storage_recovery_read_pool")
        if failure == "busy" else
        TimeoutError("synthetic title deadline") if failure == "timeout" else
        RuntimeError("synthetic title failure")
    )
    failing_read = AsyncMock(side_effect=error)
    monkeypatch.setattr(storage, operation, failing_read)
    if path == "legacy":
        monkeypatch.setattr(storage, "list_user_transcript_content_batch", None)
    else:
        fallback = AsyncMock(side_effect=AssertionError("failed batch must not cause N+1 reads"))
        monkeypatch.setattr(storage, "get_transcript", fallback)

    response = await get_dispatcher().dispatch(
        "title-failure", "sessions.list", None, context(manager),
    )

    assert response.ok is False
    assert response.payload is None
    assert response.error is not None
    assert response.error.code == ("INTERNAL_ERROR" if failure == "unexpected" else "STORAGE_BUSY")
    if failure != "unexpected":
        assert response.error.retryable is True
        assert response.error.retry_after_ms == (321 if failure == "busy" else 250)
        assert response.error.details["operation"] == operation
        assert response.error.details["stage"] == ("permit" if failure == "busy" else "deadline")
        assert response.error.details["waited_ms"] >= 0
        if failure == "busy":
            assert response.error.details["resource"] == "session_storage_recovery_read_pool"
            assert response.error.details["waited_ms"] == 17
    assert failing_read.await_count == 1
    if path != "legacy":
        fallback.assert_not_awaited()


async def test_successful_empty_title_batch_is_authoritative(
    manager: SessionManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = manager._storage
    batch = AsyncMock(return_value={})
    fallback = AsyncMock(side_effect=AssertionError("empty batch must not cause N+1 reads"))
    monkeypatch.setattr(storage, "list_user_transcript_content_batch", batch)
    monkeypatch.setattr(storage, "get_transcript", fallback)
    response = await get_dispatcher().dispatch(
        "empty-titles", "sessions.list", None, context(manager)
    )
    assert response.ok, response.error
    assert len(response.payload["sessions"]) == 3
    batch.assert_awaited_once()
    fallback.assert_not_awaited()


async def test_adapter_without_batch_capability_can_still_read_titles(
    manager: SessionManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager._storage, "list_user_transcript_content_batch", None)
    response = await get_dispatcher().dispatch(
        "legacy-titles", "sessions.list", None, context(manager)
    )
    assert response.ok, response.error
    assert {row["title"] for row in response.payload["sessions"]} == {
        f"Synthetic title {index}" for index in range(3)
    }


@pytest.mark.parametrize("field", ["display_name", "derived_title", "subject"])
async def test_authoritative_titles_do_not_need_a_reader_when_pool_is_full(
    manager: SessionManager, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    storage = manager._storage
    for node in await storage.list_sessions():
        await manager.update(node.session_key, **{
            "display_name": None, field: "Stored authoritative title",
        })
    batch = AsyncMock(side_effect=AssertionError("authoritative title needs no body read"))
    monkeypatch.setattr(storage, "list_user_transcript_content_batch", batch)
    pool = RecoveryReadPool(
        storage._open_recovery_reader, initialize=storage._initialize_recovery_reader
    )
    storage._recovery_read_pool = pool
    release = asyncio.Event()
    all_entered = asyncio.Event()
    count = 0

    async def hold(reader):
        nonlocal count
        count += 1
        if count == 4:
            all_entered.set()
        await release.wait()

    async def occupy(index):
        with recovery_read_scope(f"synthetic-authority:{index}",
                                 deadline=time.monotonic() + 5, workload="identity") as budget:
            await pool.run(budget, hold)

    readers = [asyncio.create_task(occupy(index)) for index in range(4)]
    try:
        await asyncio.wait_for(all_entered.wait(), 2)
        response = await get_dispatcher().dispatch(
            "stored-titles", "sessions.list", None, context(manager)
        )
        assert response.ok, response.error
        assert {row["title"] for row in response.payload["sessions"]} == {
            "Stored authoritative title"
        }
        assert pool.active_count == 4
        batch.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(*readers)


@pytest.mark.parametrize("failed_read", [1, 2])
async def test_fork_title_failure_happens_before_child_creation(
    manager: SessionManager, monkeypatch: pytest.MonkeyPatch, failed_read: int,
) -> None:
    storage = manager._storage
    before = await storage.list_sessions()
    good = {node.session_id: ["Synthetic title"] for node in before}
    busy = StorageBusyError("session_titles", waited_ms=0, retry_after_ms=100, stage="permit")
    batch = AsyncMock(side_effect=[good] * (failed_read - 1) + [busy])
    branch = AsyncMock(wraps=manager.branch)
    monkeypatch.setattr(storage, "list_user_transcript_content_batch", batch)
    monkeypatch.setattr(manager, "branch", branch)

    response = await get_dispatcher().dispatch(
        "fork-title-busy", "sessions.fork", {"key": before[0].session_key}, context(manager),
    )

    assert not response.ok
    assert response.error.code == "STORAGE_BUSY"
    assert response.error.retryable is True
    branch.assert_not_awaited()
    assert await storage.list_sessions() == before
