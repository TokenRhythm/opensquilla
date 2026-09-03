"""Tests that _drain_task_runtime_for_reset is called on every reset branch.

Asserts that ``_drain_task_runtime_for_reset`` is invoked regardless of
whether ``flush_service`` is None or wired, and regardless of the
``force`` flag.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import opensquilla.gateway.rpc_sessions  # noqa: F401 — ensures handler registration
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher

_ADMIN_PRINCIPAL = Principal(
    role="operator",
    scopes=frozenset({"operator.admin", "operator.write"}),
    is_owner=True,
    authenticated=True,
)

_SESSION_KEY = "agent:main:drain-test"
_SESSION_ID = "drain-test"


@dataclass
class _FakeSession:
    session_key: str = _SESSION_KEY
    session_id: str = _SESSION_ID
    agent_id: str = "main"
    status: str = "idle"
    created_at: int = 0
    updated_at: int = 0
    display_name: str | None = None
    derived_title: str | None = None
    channel: str | None = None
    chat_type: str = "unknown"
    group_id: str | None = None
    subject: str | None = None
    last_channel: str | None = None
    last_to: str | None = None
    last_account_id: str | None = None
    last_thread_id: str | None = None
    delivery_context: dict | None = None
    parent_session_key: str | None = None
    spawned_by: str | None = None
    origin: dict | None = None
    model: str | None = None
    model_override: str | None = None


class _FakeStorage:
    def __init__(self) -> None:
        self._sessions: dict[str, _FakeSession] = {_SESSION_KEY: _FakeSession()}
        self._transcripts: dict[str, list] = {}

    async def get_session(self, key: str) -> _FakeSession | None:
        return self._sessions.get(key)

    async def delete_transcript(self, session_id: str) -> None:
        self._transcripts.pop(session_id, None)


class _FakeSessionManager:
    def __init__(self) -> None:
        self._storage = _FakeStorage()
        self.applied_intents: list[tuple[str, str]] = []

    async def get_transcript(self, key: str) -> list:
        return []

    async def apply_intent(self, key: str, intent: object, **kwargs):
        self.applied_intents.append((key, str(intent)))
        session = await self._storage.get_session(key)
        if session is None:
            raise KeyError(key)
        old_id = session.session_id
        session.session_id = f"{old_id}-rotated"
        return session, True


def _make_ctx(flush_service=None, task_runtime=None) -> RpcContext:
    ctx = RpcContext(
        conn_id="test-drain",
        principal=_ADMIN_PRINCIPAL,
        config=GatewayConfig(),
    )
    ctx.session_manager = _FakeSessionManager()
    ctx.flush_service = flush_service
    ctx.task_runtime = task_runtime
    return ctx


def _make_task_runtime() -> SimpleNamespace:
    """Minimal task_runtime double with cancel() and no list/wait (non-listing path)."""
    rt = SimpleNamespace()
    rt.cancel = AsyncMock(return_value=0)
    # No `list` or `wait` attributes → has_runtime_listing=False path
    return rt


@pytest.mark.asyncio
async def test_drain_called_when_flush_service_none():
    """drain is called even when flush_service is None (kill-switch path)."""
    task_runtime = _make_task_runtime()
    ctx = _make_ctx(flush_service=None, task_runtime=task_runtime)

    target = "opensquilla.gateway.rpc_sessions._drain_task_runtime_for_reset"
    with patch(target, new_callable=AsyncMock) as mock_drain:
        result = await get_dispatcher().dispatch(
            "r1",
            "sessions.reset",
            {"key": _SESSION_KEY},
            ctx,
        )

    assert result.error is None, result.error
    mock_drain.assert_awaited_once_with(task_runtime, _SESSION_KEY)


@pytest.mark.asyncio
async def test_drain_called_with_flush_service():
    """drain is called when flush_service is wired (normal path)."""
    from opensquilla.memory.session_flush import FlushReceipt

    task_runtime = _make_task_runtime()

    flush_receipt = FlushReceipt(
        mode="skipped",
        flushed_paths=[],
        slug=None,
        message_count=0,
        duration_ms=0,
        raw_reason=None,
        error=None,
    )
    fake_flush_service = SimpleNamespace(
        execute=AsyncMock(return_value=flush_receipt),
    )

    ctx = _make_ctx(flush_service=fake_flush_service, task_runtime=task_runtime)

    target = "opensquilla.gateway.rpc_sessions._drain_task_runtime_for_reset"
    with patch(target, new_callable=AsyncMock) as mock_drain:
        result = await get_dispatcher().dispatch(
            "r1",
            "sessions.reset",
            {"key": _SESSION_KEY},
            ctx,
        )

    assert result.error is None, result.error
    mock_drain.assert_awaited_once_with(task_runtime, _SESSION_KEY)


@pytest.mark.asyncio
async def test_drain_failure_aborts_reset_without_rotating_session():
    """A writer that cannot drain leaves the durable session owner unchanged."""
    task_runtime = _make_task_runtime()
    ctx = _make_ctx(flush_service=None, task_runtime=task_runtime)

    target = "opensquilla.gateway.rpc_sessions._drain_task_runtime_for_reset"
    with patch(
        target,
        new_callable=AsyncMock,
        side_effect=TimeoutError("synthetic stubborn writer"),
    ) as mock_drain:
        result = await get_dispatcher().dispatch(
            "r1",
            "sessions.reset",
            {"key": _SESSION_KEY},
            ctx,
        )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "session_reset_busy"
    assert result.error.retryable is True
    assert result.error.details == {
        "key": _SESSION_KEY,
        "phase": "task_runtime_drain",
    }
    assert ctx.session_manager.applied_intents == []
    current = await ctx.session_manager._storage.get_session(_SESSION_KEY)
    assert current is not None
    assert current.session_id == _SESSION_ID
    mock_drain.assert_awaited_once_with(task_runtime, _SESSION_KEY)


@pytest.mark.asyncio
async def test_reset_holds_all_writer_fences_through_snapshot_and_rotation():
    """The destructive window begins only after every writer has drained."""
    order: list[str] = []
    active_fences: set[str] = set()

    @asynccontextmanager
    async def fence(name: str, keys):
        assert tuple(keys) == (_SESSION_KEY,)
        order.append(f"{name}:enter")
        active_fences.add(name)
        try:
            yield
        finally:
            active_fences.remove(name)
            order.append(f"{name}:exit")

    class _WriteLock:
        async def __aenter__(self):
            order.append("write:enter")
            active_fences.add("write")
            return self

        async def __aexit__(self, *_args):
            active_fences.remove("write")
            order.append("write:exit")

    async def drain_router(keys) -> None:
        assert tuple(keys) == (_SESSION_KEY,)
        assert active_fences == {"background", "runtime", "direct", "write"}
        order.append("router:drain")

    async def drain_turn(keys) -> None:
        assert tuple(keys) == (_SESSION_KEY,)
        assert active_fences == {"background", "runtime", "direct", "write"}
        order.append("turn:drain")

    @asynccontextmanager
    async def runtime_fence(
        keys,
        *,
        cancel_source: str,
        cancel_reason: str,
    ):
        assert cancel_source == "sessions_reset"
        assert cancel_reason == "session_reset"
        async with fence("runtime", keys):
            yield

    task_runtime = _make_task_runtime()
    task_runtime.quiesce_sessions = runtime_fence
    ctx = _make_ctx(flush_service=None, task_runtime=task_runtime)
    manager = ctx.session_manager
    original_get_transcript = manager.get_transcript
    original_apply_intent = manager.apply_intent

    async def observed_get_transcript(key: str) -> list:
        assert active_fences == {"background", "runtime", "direct", "write"}
        order.append("snapshot")
        return await original_get_transcript(key)

    async def observed_apply_intent(key: str, intent: object, **kwargs):
        assert active_fences == {"background", "runtime", "direct", "write"}
        order.append("rotate")
        return await original_apply_intent(key, intent, **kwargs)

    manager.get_transcript = observed_get_transcript
    manager.apply_intent = observed_apply_intent
    ctx.turn_runner = SimpleNamespace(
        get_session_lock=lambda _key: _WriteLock(),
        drain_session_background_writes=drain_turn,
    )

    with (
        patch(
            "opensquilla.gateway.rpc_sessions._drain_task_runtime_for_reset",
            new_callable=AsyncMock,
        ),
        patch(
            "opensquilla.gateway.rpc_sessions.quiesce_background_completion_sessions",
            side_effect=lambda keys: fence("background", keys),
        ),
        patch(
            "opensquilla.gateway.rpc_sessions.get_agent_task_registry",
            return_value=SimpleNamespace(
                quiesce_sessions=lambda keys: fence("direct", keys)
            ),
        ),
        patch(
            "opensquilla.gateway.rpc_sessions.drain_pending_flushes_for_sessions",
            side_effect=drain_router,
        ),
    ):
        result = await get_dispatcher().dispatch(
            "r1",
            "sessions.reset",
            {"key": _SESSION_KEY},
            ctx,
        )

    assert result.error is None, result.error
    assert order == [
        "background:enter",
        "runtime:enter",
        "direct:enter",
        "write:enter",
        "router:drain",
        "turn:drain",
        "snapshot",
        "rotate",
        "write:exit",
        "direct:exit",
        "runtime:exit",
        "background:exit",
    ]
