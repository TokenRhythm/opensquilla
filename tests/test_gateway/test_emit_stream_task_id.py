"""Issue #344: every TaskRuntime stream event must carry its ``task_id``.

Without a task id on ``session.event.*`` payloads the WebUI cannot tell a
stale turn's late ``tool_use_start`` / ``error`` / ``done`` from the current
turn's, so they leak into whatever turn is on screen. These tests pin the
backend half of the fix: the emitter stamps ``task_id`` on every payload, the
dispatcher threads ``run.task_id`` into the emitter, and the field stays absent
when no task id is supplied (old-client compatibility).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.types import ErrorEvent, TextDeltaEvent, ToolUseStartEvent
from opensquilla.gateway.boot import (
    TaskRuntimeStreamError,
    _emit_task_runtime_stream_events,
    dispatch_task_runtime_turn,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import _task_identity_payload

SESSION = "agent:main:webchat:issue344"


def _make_envelope(session_key: str = SESSION) -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id="main",
        session_key=session_key,
        input_provenance={"kind": "test"},
        metadata={},
    )


def test_task_identity_keeps_client_and_durable_message_ids_distinct() -> None:
    envelope = _make_envelope()
    envelope.metadata.update({"client_message_id": "client-message-A", "surface_id": "web:browser"})

    payload = _task_identity_payload(
        envelope,
        "turn-A",
        user_message_id="durable-message-A",
    )

    assert payload == {
        "turn_id": "turn-A",
        "client_message_id": "client-message-A",
        "user_message_id": "durable-message-A",
        "surface_id": "web:browser",
    }


@pytest.mark.asyncio
async def test_emit_stamps_task_id_on_every_stream_event() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _stream():
        yield ToolUseStartEvent(tool_use_id="t1", tool_name="create_pdf.py")
        yield TextDeltaEvent(text="partial output")

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    await _emit_task_runtime_stream_events(
        _stream(),
        SESSION,
        _emitter,
        idle_timeout=5.0,
        heartbeat_interval=0.0,
        task_id="task-A",
    )

    assert [name for _, name, _ in emitted] == [
        "session.event.tool_use_start",
        "session.event.text_delta",
    ]
    assert all(payload.get("task_id") == "task-A" for _, _, payload in emitted)


@pytest.mark.asyncio
async def test_emit_stamps_cross_surface_turn_identity_on_every_stream_event() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _stream():
        yield TextDeltaEvent(text="partial output")

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    await _emit_task_runtime_stream_events(
        _stream(),
        SESSION,
        _emitter,
        idle_timeout=5.0,
        heartbeat_interval=0.0,
        task_id="task-A",
        session_id="session-A",
        client_message_id="client-message-A",
        user_message_id="durable-message-A",
        surface_id="tui:process-A",
    )

    payload = emitted[0][2]
    assert payload["text"] == "partial output"
    assert payload["task_id"] == "task-A"
    assert payload["turn_id"] == "task-A"
    assert payload["session_id"] == "session-A"
    assert payload["client_message_id"] == "client-message-A"
    assert payload["user_message_id"] == "durable-message-A"
    assert payload["surface_id"] == "tui:process-A"


@pytest.mark.asyncio
async def test_emit_stamps_task_id_on_terminal_error_event() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _stream():
        yield ErrorEvent(message="boom", code="tool_error")

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    with pytest.raises(TaskRuntimeStreamError):
        await _emit_task_runtime_stream_events(
            _stream(),
            SESSION,
            _emitter,
            idle_timeout=5.0,
            heartbeat_interval=0.0,
            task_id="task-A",
        )

    assert emitted, "the error event should still have been emitted before raising"
    session_key, event_name, payload = emitted[-1]
    assert event_name == "session.event.error"
    assert payload["task_id"] == "task-A"


@pytest.mark.asyncio
async def test_emit_without_task_id_omits_field_for_old_clients() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _stream():
        yield ToolUseStartEvent(tool_use_id="t1", tool_name="shell")

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    await _emit_task_runtime_stream_events(
        _stream(),
        SESSION,
        _emitter,
        idle_timeout=5.0,
        heartbeat_interval=0.0,
    )

    assert emitted
    assert "task_id" not in emitted[0][2]


@pytest.mark.asyncio
async def test_dispatch_threads_run_task_id_into_stream_events() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    class _Runner:
        async def run(self, message: str, session_key: str, **kwargs: Any):  # noqa: ARG002
            yield ToolUseStartEvent(tool_use_id="t1", tool_name="shell")
            yield TextDeltaEvent(text="hi")

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    run = SimpleNamespace(
        agent_id="main",
        task_id="task-77",
        session_key=SESSION,
        message="hello",
        envelope=_make_envelope(),
        attachments=[],
        input_provenance={},
        run_kind="interactive",
        no_memory_capture=False,
        ingress_pipeline_steps=[],
        semantic_message=None,
        persisted_user_message_id=None,
        stream_event_sink=None,
    )

    await dispatch_task_runtime_turn(
        run,
        config=GatewayConfig(
            agent_stream_heartbeat_interval_seconds=0.0,
            agent_stream_idle_timeout_seconds=5.0,
        ),
        session_manager=None,
        turn_runner=_Runner(),
        event_emitter=_emitter,
    )

    stream_events = [e for e in emitted if e[1].startswith("session.event.")]
    assert stream_events, "the dispatcher should have emitted stream events"
    assert all(payload.get("task_id") == "task-77" for _, _, payload in stream_events)
