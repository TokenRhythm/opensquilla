"""Offline physical fallback facts through public events and reopened history."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.gateway.boot import _emit_task_runtime_stream_events
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_chat import _handle_chat_history
from opensquilla.gateway.session_streams import SessionStreamRegistry
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.provider import DoneEvent, ErrorEvent, TextDeltaEvent
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskStatus
from opensquilla.session.storage import SessionStorage


@pytest.mark.parametrize("target_succeeds", [True, False])
@pytest.mark.parametrize("source_model", ["physical-A", ""])
async def test_fallback_identity_survives_gateway_and_reopened_history(
    tmp_path, monkeypatch, target_succeeds, source_model,
) -> None:
    session_key = "agent:main:webchat:fallback-identity"
    emitted = []
    calls = []
    registry = SessionStreamRegistry()
    monkeypatch.setattr("opensquilla.gateway.session_streams._session_streams", registry)

    class Provider:
        provider_name = "openrouter"

        def __init__(self, model, *, primary=False):
            self.active_model_id = model
            self.primary = primary

        async def chat(self, messages, tools=None, config=None):
            del messages, tools, config
            calls.append(self.active_model_id)
            if self.primary:
                yield ErrorEvent(message="synthetic unavailable", code="503")
                return
            # A target label is an attempt, even if this request later fails.
            fallback = next(
                payload for name, payload in emitted
                if name == "session.event.provider_activity" and payload["phase"] == "fallback"
            )
            assert fallback["model"] == "physical-B"
            assert fallback.get("from_model", "") == source_model
            if target_succeeds:
                yield TextDeltaEvent(text="Synthetic answer")
                yield DoneEvent(model=self.active_model_id)
            else:
                yield ErrorEvent(message="synthetic unavailable", code="auth_invalid")

    target = Provider("physical-B")

    class Selector:
        current_config = SimpleNamespace(provider="openrouter", model="planned-A")

        def next_fallback_after_failure(self, exc):
            del exc
            self.current_config = SimpleNamespace(provider="openrouter", model="planned-B")
            return target

    agent = Agent(
        _SelectorFallbackProvider(Provider(source_model, primary=True), Selector()),
        AgentConfig(
            max_provider_retries=0, model_id="configured-model", workspace_dir=str(tmp_path),
        ),
    )
    database_path = str(tmp_path / "fallback-history.sqlite")
    storage = await SessionStorage.open(database_path)
    manager = SessionManager(storage, inject_time_prefix=False)

    async def emit(key, name, payload):
        if name.startswith("session.event."):
            payload = registry.record(key, name, payload)
        emitted.append((name, payload))

    async def handler(run):
        await manager.append_message(session_key, "user", "Synthetic request")
        await _emit_task_runtime_stream_events(
            agent.run_turn("Synthetic request"), session_key, emit,
            task_id=run.task_id, session_id=session.session_id, session_epoch=session.epoch,
            heartbeat_interval=0.0, idle_timeout=2.0,
        )

    runtime = TaskRuntime(storage=storage, turn_handler=handler, event_emitter=emit)
    try:
        session = await manager.create(session_key)
        handle = await runtime.enqueue(RouteEnvelope(
            source_kind=SourceKind.WEB, source_name="fallback-identity",
            agent_id="main", session_key=session_key, session_id=session.session_id,
            session_epoch=session.epoch, input_provenance={"kind": "test"},
        ), "Synthetic request")
        record = await runtime.wait(handle.task_id, timeout=5.0)
    finally:
        await runtime.shutdown(cancel=False)
        await storage.close()

    assert calls == [source_model, "physical-B"]
    assert record.status == (
        AgentTaskStatus.SUCCEEDED if target_succeeds else AgentTaskStatus.FAILED
    )
    live = [
        payload for name, payload in emitted
        if name == "session.event.provider_activity" and payload["phase"] == "fallback"
    ]
    assert len(live) == 1
    assert live[0]["model"] == "physical-B"
    assert live[0].get("from_model") == (source_model or None)
    assert "succeeded" not in live[0] and "success" not in live[0]

    reopened = await SessionStorage.open(database_path)
    try:
        recovered = await reopened.get_agent_task(handle.task_id)
        assert recovered is not None
        snapshot = recovered.details["activity_snapshot"]
        stored = [entry for entry in snapshot["entries"] if entry.get("phase") == "fallback"]
        assert len(stored) == 1
        assert stored[0]["model"] == live[0]["model"]
        assert stored[0].get("from_model") == live[0].get("from_model")
        history = await _handle_chat_history(
            {"sessionKey": session_key, "limit": 10},
            RpcContext(
                conn_id="fallback-identity", principal=SimpleNamespace(role="operator"),
                session_manager=SessionManager(reopened, inject_time_prefix=False),
            ),
        )
        assert history["turn_outcomes"][0]["activity_snapshot"] == snapshot
    finally:
        await reopened.close()
