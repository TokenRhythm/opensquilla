"""Atomic RPC contracts for WebChat prefix-fork turn acceptance."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.session.turn_context import turn_context_scope

PARENT_KEY = "agent:main:webchat:atomic-fork"
CLIENT_REQUEST_ID = "atomic-fork-request"

_PRINCIPAL = Principal(
    role="operator",
    scopes=frozenset(["operator.admin"]),
    is_owner=True,
    authenticated=True,
)


@dataclass
class _ForkStack:
    db_path: Path
    storage: SessionStorage
    manager: SessionManager
    runtime: TaskRuntime
    context: RpcContext
    handler_started: asyncio.Event
    release_handler: asyncio.Event

    async def wait_until_running(self) -> None:
        await asyncio.wait_for(self.handler_started.wait(), timeout=2.0)


@asynccontextmanager
async def _open_fork_stack(db_path: Path) -> AsyncIterator[_ForkStack]:
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage, inject_time_prefix=False)
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()

    async def _turn_handler(_run: Any) -> None:
        handler_started.set()
        await release_handler.wait()

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_turn_handler,
        max_concurrency=1,
        running_heartbeat_interval_s=None,
    )
    context = RpcContext(
        conn_id="atomic-fork-test",
        principal=_PRINCIPAL,
        config=GatewayConfig(
            workspace_dir=str(db_path.parent / "workspace"),
            memory={"flush_enabled": False},
            naming={"enabled": False},
        ),
        session_manager=manager,
        task_runtime=runtime,
    )
    stack = _ForkStack(
        db_path=db_path,
        storage=storage,
        manager=manager,
        runtime=runtime,
        context=context,
        handler_started=handler_started,
        release_handler=release_handler,
    )
    try:
        yield stack
    finally:
        release_handler.set()
        for reservations in list(runtime._reservations_by_session.values()):
            for reservation in list(reservations):
                await runtime.abort_reservation(reservation)
        await runtime.shutdown(cancel=True, timeout=2.0)
        await storage.close()


async def _seed_parent(stack: _ForkStack) -> str:
    await stack.manager.create(
        PARENT_KEY,
        agent_id="main",
        display_name="Atomic fork parent",
    )
    with turn_context_scope(
        {
            "turn_id": "parent-turn-a",
            "client_message_id": "parent-message-a",
            "surface_id": "web:parent",
            "intent": "send",
            "disposition": "applied",
            "revision": 1,
        }
    ):
        await stack.manager.append_message(PARENT_KEY, "user", "A marker")
    middle = await stack.manager.append_message(PARENT_KEY, "assistant", "B marker")
    await stack.manager.append_message(PARENT_KEY, "user", "C marker")
    return middle.message_id


def _table_counts(db_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(db_path)
    try:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "sessions",
                "transcript_entries",
                "agent_tasks",
                "turn_ingress_receipts",
            )
        }
    finally:
        connection.close()


def _fork_params(
    fork_before_message_id: str,
    *,
    fork_param_name: str = "forkBeforeMessageId",
) -> dict[str, str]:
    return {
        "sessionKey": PARENT_KEY,
        "message": "B edited",
        fork_param_name: fork_before_message_id,
        "clientRequestId": CLIENT_REQUEST_ID,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("fork_param_name", ["forkBeforeMessageId", "fork_before_message_id"])
async def test_chat_send_fork_atomically_accepts_child_prefix_message_task_and_receipt(
    tmp_path: Path,
    fork_param_name: str,
) -> None:
    async with _open_fork_stack(tmp_path / "sessions.db") as stack:
        fork_before_message_id = await _seed_parent(stack)

        response = await get_dispatcher().dispatch(
            "rpc-fork-success",
            "chat.send",
            _fork_params(fork_before_message_id, fork_param_name=fork_param_name),
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert response.payload["accepted"] is True
        assert response.payload["replayed"] is False
        child_key = response.payload["sessionKey"]
        assert child_key != PARENT_KEY

        parent_entries = await stack.manager.get_transcript(PARENT_KEY)
        assert [entry.content for entry in parent_entries] == [
            "A marker",
            "B marker",
            "C marker",
        ]
        child = await stack.storage.get_session(child_key)
        assert child is not None
        assert child.parent_session_key == PARENT_KEY
        assert child.forked_from_parent is True
        child_entries = await stack.manager.get_transcript(child_key)
        assert [entry.content for entry in child_entries] == ["A marker", "B edited"]
        assert child_entries[0].turn_context == {
            "turn_id": "parent-turn-a",
            "client_message_id": "parent-message-a",
            "surface_id": "web:parent",
            "intent": "send",
            "disposition": "applied",
            "revision": 1,
        }
        assert child_entries[-1].turn_context == {
            "turn_id": response.payload["task_id"],
            "client_request_id": "atomic-fork-request",
            "client_message_id": response.payload["client_message_id"],
            "surface_id": response.payload["surface_id"],
            "intent": "send",
            "disposition": "applied",
            "revision": 1,
        }
        assert child_entries[-1].message_id == response.payload["message_id"]
        assert response.payload["user_message_id"] == response.payload["message_id"]
        assert response.payload["turn_id"] == response.payload["task_id"]
        assert isinstance(response.payload["client_message_id"], str)
        assert response.payload["client_message_id"]
        assert response.payload["surface_id"].startswith("webchat:")

        task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert task.session_key == child_key
        assert task.details["persisted_user_message_id"] == child_entries[-1].message_id
        assert task.details["fresh_user_session"] is False
        receipt = await stack.storage.get_turn_ingress_receipt(
            source_scope="web:webchat:operator",
            request_session_key=PARENT_KEY,
            client_request_id=CLIENT_REQUEST_ID,
        )
        assert receipt is not None
        assert receipt.receipt.accepted_session_key == child_key
        assert receipt.receipt.session_id == child.session_id
        assert receipt.receipt.message_id == child_entries[-1].message_id
        assert receipt.receipt.task_id == task.task_id
        assert _table_counts(stack.db_path) == {
            "sessions": 2,
            "transcript_entries": 5,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_chat_send_fork_replays_same_child_without_duplicate_side_effects(
    tmp_path: Path,
) -> None:
    async with _open_fork_stack(tmp_path / "sessions.db") as stack:
        fork_before_message_id = await _seed_parent(stack)
        params = _fork_params(fork_before_message_id)

        first = await get_dispatcher().dispatch(
            "rpc-fork-first",
            "chat.send",
            params,
            stack.context,
        )
        await stack.wait_until_running()
        replay = await get_dispatcher().dispatch(
            "rpc-fork-replay",
            "chat.send",
            params,
            stack.context,
        )

        assert first.ok is True
        assert replay.ok is True
        assert replay.payload["accepted"] is True
        assert replay.payload["replayed"] is True
        assert replay.payload["sessionKey"] == first.payload["sessionKey"]
        assert replay.payload["session_id"] == first.payload["session_id"]
        assert replay.payload["message_id"] == first.payload["message_id"]
        assert replay.payload["task_id"] == first.payload["task_id"]
        assert [
            entry.content
            for entry in await stack.manager.get_transcript(replay.payload["sessionKey"])
        ] == ["A marker", "B edited"]
        assert _table_counts(stack.db_path) == {
            "sessions": 2,
            "transcript_entries": 5,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_chat_send_fork_storage_busy_leaves_no_child_turn_or_reservation(
    tmp_path: Path,
) -> None:
    async with _open_fork_stack(tmp_path / "sessions.db") as stack:
        fork_before_message_id = await _seed_parent(stack)
        stack.storage._busy_budget_seconds = 0.0
        await stack.storage.conn.execute("PRAGMA busy_timeout = 0")
        external_writer = sqlite3.connect(stack.db_path, isolation_level=None, timeout=0.0)
        external_writer.execute("BEGIN IMMEDIATE")
        try:
            response = await get_dispatcher().dispatch(
                "rpc-fork-busy",
                "chat.send",
                _fork_params(fork_before_message_id),
                stack.context,
            )

            assert response.ok is False
            assert response.error is not None
            assert response.error.code == "STORAGE_BUSY"
            assert response.error.retryable is True
            assert response.error.accepted is False
            assert response.error.retry_after_ms is not None
            assert [entry.content for entry in await stack.manager.get_transcript(PARENT_KEY)] == [
                "A marker",
                "B marker",
                "C marker",
            ]
            assert _table_counts(stack.db_path) == {
                "sessions": 1,
                "transcript_entries": 3,
                "agent_tasks": 0,
                "turn_ingress_receipts": 0,
            }
            assert stack.runtime._reservations_by_session == {}
            assert stack.runtime._tasks == {}
            assert stack.runtime._pending_by_session == {}
            assert stack.runtime._running_by_session == {}
            assert stack.handler_started.is_set() is False
        finally:
            external_writer.execute("ROLLBACK")
            external_writer.close()
