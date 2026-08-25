"""Regression tests for chat.send middle-message edit branching."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio

from opensquilla.engine.types import DoneEvent
from opensquilla.gateway.agent_tasks import get_agent_task_registry
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

_PRINCIPAL = Principal(
    role="operator", scopes=frozenset(["operator.admin"]), is_owner=True, authenticated=True
)

PARENT_KEY = "agent:main:webchat:editbranch01"


class _RecordingTurnRunner:
    def __init__(self) -> None:
        self.run_calls: list[dict[str, Any]] = []
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def run(self, message: str, session_key: str, **kwargs: Any):
        self.run_calls.append({"message": message, "session_key": session_key, **kwargs})
        yield DoneEvent()


@pytest.fixture
def dispatcher():
    return get_dispatcher()


@pytest_asyncio.fixture
async def manager():
    storage = SessionStorage(":memory:")
    await storage.connect()
    mgr = SessionManager(storage, inject_time_prefix=False)
    yield mgr
    await storage.close()


@pytest.fixture
def ctx(manager):
    runner = _RecordingTurnRunner()
    context = RpcContext(
        conn_id="test-conn",
        principal=_PRINCIPAL,
        config=GatewayConfig(memory={"flush_enabled": False}),
        turn_runner=runner,
    )
    context.session_manager = manager
    return context


@pytest.mark.asyncio
async def test_chat_send_fork_before_message_returns_child_without_future_history(
    dispatcher,
    ctx,
    manager,
):
    await manager.create(PARENT_KEY, agent_id="main", display_name="Branch edit")
    await manager.append_message(PARENT_KEY, "user", "A marker", token_count=1)
    middle = await manager.append_message(PARENT_KEY, "user", "B marker", token_count=1)
    await manager.append_message(PARENT_KEY, "user", "C marker", token_count=1)

    res = await dispatcher.dispatch(
        "r-chat-branch-edit",
        "chat.send",
        {
            "sessionKey": PARENT_KEY,
            "message": "B edited",
            "forkBeforeMessageId": middle.message_id,
        },
        ctx,
    )

    task_key = res.payload.get("sessionKey", PARENT_KEY) if res.ok else PARENT_KEY
    task = get_agent_task_registry().get(task_key) or get_agent_task_registry().get(PARENT_KEY)
    if task is not None:
        await task

    assert res.ok is True
    child_key = res.payload["sessionKey"]
    assert child_key != PARENT_KEY
    assert res.payload["key"] == child_key
    child = await manager.get_session(child_key)
    assert child is not None
    assert child.display_name == "Branch edit (2)"

    parent_entries = await manager.get_transcript(PARENT_KEY)
    assert [entry.content for entry in parent_entries] == [
        "A marker",
        "B marker",
        "C marker",
    ]

    child_entries = await manager.get_transcript(child_key)
    assert [entry.content for entry in child_entries] == ["A marker", "B edited"]

    assert ctx.turn_runner.run_calls[0]["session_key"] == child_key
    assert ctx.turn_runner.run_calls[0]["message"] == "B edited"
