"""Tests that runtime quiescence is called on every reset branch.

Asserts that the concrete reset quiescence Port is invoked regardless of
whether ``flush_service`` is None or wired, and regardless of the force flag.
"""

from __future__ import annotations

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

    target = (
        "opensquilla.gateway.adapters.session_reset."
        "GatewaySessionResetPorts._quiesce_task_runtime"
    )
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

    target = (
        "opensquilla.gateway.adapters.session_reset."
        "GatewaySessionResetPorts._quiesce_task_runtime"
    )
    with patch(target, new_callable=AsyncMock) as mock_drain:
        result = await get_dispatcher().dispatch(
            "r1",
            "sessions.reset",
            {"key": _SESSION_KEY},
            ctx,
        )

    assert result.error is None, result.error
    mock_drain.assert_awaited_once_with(task_runtime, _SESSION_KEY)
