"""Legacy ingress retains the same route-ready title handoff."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.engine.types import DoneEvent
from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.agent_tasks import get_agent_task_registry
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.task_runtime import TaskQueueFullError
from opensquilla.provider.selector import ProviderConfig
from tests.test_gateway.test_rpc_sessions import FakeSession, FakeSessionManager, make_ctx


@pytest.mark.parametrize("runtime_kind", ["direct", "queued", "rejected"])
async def test_legacy_ingress_installs_title_after_persistence_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, runtime_kind: str,
) -> None:
    session = FakeSession(
        session_key=f"agent:main:webchat:legacy-title-{runtime_kind}", display_name="WebChat",
        epoch=3,
    )
    manager = FakeSessionManager([session])
    monkeypatch.setattr(
        manager._storage, "count_transcript_entries", AsyncMock(return_value=0), raising=False,
    )
    scheduled = AsyncMock()
    monkeypatch.setattr(rpc_sessions, "generate_session_title", scheduled)
    selected = ProviderConfig("openai", "legacy-selected")
    runner_entered = asyncio.Event()
    release_route = asyncio.Event()
    routes = []

    class Runner:
        async def run(self, _message, _key, **kwargs):
            assert manager.created_messages
            assert not scheduled.called
            runner_entered.set()
            await release_route.wait()
            kwargs["on_initial_provider_ready"](selected)
            yield DoneEvent(text="answer")

    class Runtime:
        async def enqueue(self, envelope, _message, **kwargs):
            assert manager.created_messages
            assert not scheduled.called
            assert callable(envelope.runtime_services.get("on_initial_provider_ready"))
            routes.append(envelope)
            if runtime_kind == "rejected":
                raise TaskQueueFullError(session_key=envelope.session_key, max_pending=1)
            return SimpleNamespace(
                task_id=kwargs["task_id"], session_key=envelope.session_key, status="queued",
            )

    ctx = make_ctx(
        session_manager=manager, turn_runner=Runner(),
        task_runtime=None if runtime_kind == "direct" else Runtime(),
    )
    ctx.config.naming.enabled = True
    response = await get_dispatcher().dispatch(
        "legacy-title", "sessions.send", {"key": session.session_key, "message": "User topic"}, ctx,
    )
    if runtime_kind == "rejected":
        assert not response.ok
        assert response.error.code == "QUEUE_FULL"
        assert manager.removed_messages
        scheduled.assert_not_called()
        return
    assert response.ok, response.error
    scheduled.assert_not_called()
    if runtime_kind == "direct":
        await asyncio.wait_for(runner_entered.wait(), timeout=10)
        task = get_agent_task_registry().get(session.session_key)
        assert task is not None
        release_route.set()
        await asyncio.wait_for(task, timeout=10)
    else:
        routes[0].runtime_services["on_initial_provider_ready"](selected)
        await asyncio.sleep(0)
    scheduled.assert_awaited_once()
    assert scheduled.await_args.args[2] == "User topic"
    assert scheduled.await_args.kwargs["expected_session_id"] == session.session_id
    assert scheduled.await_args.kwargs["expected_session_epoch"] == 3


def test_title_handoff_is_consumed_even_when_scheduling_fails(monkeypatch: pytest.MonkeyPatch):
    from opensquilla.gateway.routing import RouteEnvelope, SourceKind

    route = RouteEnvelope(SourceKind.WEB, "web", "main", "agent:main:webchat:one")
    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("synthetic scheduling failure")

    monkeypatch.setattr(rpc_sessions, "_schedule_auto_title", fail)
    rpc_sessions._install_auto_title(
        SimpleNamespace(), route, route.session_key, "User topic", enabled=True,
        session_id="session-one", session_epoch=0, root_turn_id="turn-one",
    )
    ready = route.runtime_services["on_initial_provider_ready"]
    with pytest.raises(RuntimeError, match="synthetic scheduling failure"):
        ready(ProviderConfig("openai", "initial"))
    ready(ProviderConfig("openai", "fallback"))
    assert len(calls) == 1
    assert calls[0]["selected_provider_config"].model == "initial"
    assert route.metadata == {}
