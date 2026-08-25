"""Issue #344: every TaskRuntime stream event must carry its ``task_id``.

Without a task id on ``session.event.*`` payloads the WebUI cannot tell a
stale turn's late ``tool_use_start`` / ``error`` / ``done`` from the current
turn's, so they leak into whatever turn is on screen. These tests pin the
backend half of the fix: the emitter stamps ``task_id`` on every payload, the
dispatcher threads ``run.task_id`` into the emitter, and the field stays absent
when no task id is supplied (old-client compatibility).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.types import (
    AnswerGenerationResetEvent,
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ThinkingEndEvent,
    ThinkingEvent,
    ThinkingStartEvent,
    ToolUseStartEvent,
)
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
        yield TextDeltaEvent(
            text="partial output",
            model_call_id="1.2",
            iteration=1,
        )

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
    assert emitted[-1][2]["model_call_id"] == "1.2"
    assert emitted[-1][2]["iteration"] == 1


@pytest.mark.asyncio
async def test_emit_preserves_thinking_start_time() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _stream():
        yield ThinkingEvent(
            text="checking",
            started_at=1_234_567,
            model_call_id="2.0",
            iteration=2,
        )

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    await _emit_task_runtime_stream_events(
        _stream(),
        SESSION,
        _emitter,
        idle_timeout=5.0,
        heartbeat_interval=0.0,
    )

    assert emitted == [
        (
            SESSION,
            "session.event.thinking",
            {
                "text": "checking",
                "started_at": 1_234_567,
                "generation_epoch": 0,
                "model_call_id": "2.0",
                "iteration": 2,
            },
        )
    ]


@pytest.mark.asyncio
async def test_emit_preserves_typed_silent_reply_done_contract() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _stream():
        yield DoneEvent(
            text="",
            text_snapshot="",
            delivery="suppressed",
            suppression_reason="no_reply",
            router_model_call_id="3.0",
            router_iteration=3,
        )

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    await _emit_task_runtime_stream_events(
        _stream(),
        SESSION,
        _emitter,
        idle_timeout=5.0,
        heartbeat_interval=0.0,
        input_mode="system_event",
        run_kind="goal",
    )

    _, event_name, payload = emitted[0]
    assert event_name == "session.event.done"
    assert payload["text_snapshot"] == ""
    assert payload["delivery"] == "suppressed"
    assert payload["suppression_reason"] == "no_reply"
    assert payload["router_model_call_id"] == "3.0"
    assert payload["router_iteration"] == 3
    assert payload["input_mode"] == "system_event"
    assert payload["run_kind"] == "goal"


@pytest.mark.asyncio
async def test_emit_preserves_reasoning_block_lifecycle() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _stream():
        yield ThinkingStartEvent(
            block_id="reasoning-1",
            block_index=0,
            started_at=1_000,
            generation_epoch=2,
        )
        yield ThinkingEvent(
            text="checking",
            started_at=1_000,
            block_id="reasoning-1",
            block_index=0,
            generation_epoch=2,
        )
        yield ThinkingEndEvent(
            block_id="reasoning-1",
            block_index=0,
            status="completed",
            ended_at=2_000,
            generation_epoch=2,
        )

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    await _emit_task_runtime_stream_events(
        _stream(),
        SESSION,
        _emitter,
        idle_timeout=5.0,
        heartbeat_interval=0.0,
    )

    assert [event_name for _, event_name, _ in emitted] == [
        "session.event.thinking_start",
        "session.event.thinking",
        "session.event.thinking_end",
    ]
    assert [payload["block_id"] for _, _, payload in emitted] == [
        "reasoning-1",
        "reasoning-1",
        "reasoning-1",
    ]
    assert emitted[1][2]["generation_epoch"] == 2
    assert emitted[0][2]["generation_epoch"] == 2
    assert emitted[2][2]["generation_epoch"] == 2


@pytest.mark.asyncio
async def test_emit_legacy_ordinary_event_without_generation_epoch_keeps_shape() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _stream():
        yield SimpleNamespace(kind="thinking", text="legacy", started_at=123)

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    await _emit_task_runtime_stream_events(
        _stream(),
        SESSION,
        _emitter,
        idle_timeout=5.0,
        heartbeat_interval=0.0,
    )

    assert emitted == [
        (SESSION, "session.event.thinking", {"text": "legacy", "started_at": 123})
    ]


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
async def test_emit_generation_reset_keeps_typed_identity_and_wire_name() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _stream():
        yield AnswerGenerationResetEvent(
            turn_id="engine-turn-1",
            assistant_message_id="assistant-message-1",
            old_generation_epoch=0,
            new_generation_epoch=1,
            safe_reason="aggregator fallback",
            sequence=17,
            authoritative_text_snapshot="authoritative answer",
            terminal_error_message="INTERNAL_ONLY_MESSAGE",
            terminal_error_code="INTERNAL_ONLY_CODE",
            terminal_failure_kind="INTERNAL_ONLY_KIND",
        )

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    await _emit_task_runtime_stream_events(
        _stream(),
        SESSION,
        _emitter,
        idle_timeout=5.0,
        heartbeat_interval=0.0,
        task_id="task-1",
    )

    assert len(emitted) == 1
    session_key, event_name, payload = emitted[0]
    assert session_key == SESSION
    assert event_name == "session.event.answer_generation_reset"
    assert payload["task_id"] == "task-1"
    assert payload["turn_id"] == "engine-turn-1"
    assert payload["assistant_message_id"] == "assistant-message-1"
    assert payload["old_generation_epoch"] == 0
    assert payload["new_generation_epoch"] == 1
    assert payload["sequence"] == 17
    assert payload["authoritative_text_snapshot"] == "authoritative answer"
    assert "terminal_error_message" not in payload
    assert "terminal_error_code" not in payload
    assert "terminal_failure_kind" not in payload


@pytest.mark.asyncio
async def test_emit_terminal_generation_reset_marks_task_failed_without_public_error() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []
    terminal_text = "The fixed model could not complete this answer."

    async def _stream():
        yield TextDeltaEvent(text="superseded partial")
        yield AnswerGenerationResetEvent(
            turn_id="engine-turn-terminal-reset",
            assistant_message_id="assistant-terminal-reset",
            old_generation_epoch=1,
            new_generation_epoch=2,
            safe_reason="fixed provider final failure",
            sequence=8,
            terminal=True,
            terminal_text_snapshot=terminal_text,
            terminal_error_message="safe internal failure",
            terminal_error_code="provider_unavailable",
            terminal_failure_kind="upstream_unavailable",
        )

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    with pytest.raises(TaskRuntimeStreamError) as exc_info:
        await _emit_task_runtime_stream_events(
            _stream(),
            SESSION,
            _emitter,
            idle_timeout=5.0,
            heartbeat_interval=0.0,
            task_id="task-terminal-reset",
        )

    assert str(exc_info.value) == terminal_text
    assert exc_info.value.code == "provider_unavailable"
    assert exc_info.value.terminal_reason == "error"
    assert exc_info.value.failure_kind == "upstream_unavailable"
    assert [name for _session, name, _payload in emitted] == [
        "session.event.text_delta",
        "session.event.answer_generation_reset",
    ]
    assert emitted[-1][2]["terminal"] is True
    assert emitted[-1][2]["terminal_reason"] == "error"
    assert "terminal_error_message" not in emitted[-1][2]
    assert "terminal_error_code" not in emitted[-1][2]
    assert "terminal_failure_kind" not in emitted[-1][2]


@pytest.mark.asyncio
async def test_context_bound_timeout_cannot_emit_second_terminal_error() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _stream():
        await asyncio.sleep(0.04)
        yield AnswerGenerationResetEvent(
            turn_id="engine-turn-context-bound",
            assistant_message_id="assistant-context-bound",
            old_generation_epoch=0,
            new_generation_epoch=1,
            safe_reason="canonical ensemble takeover",
            sequence=9,
        )
        yield DoneEvent(text="fixed answer")

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    await _emit_task_runtime_stream_events(
        _stream(),
        SESSION,
        _emitter,
        idle_timeout=0.001,
        heartbeat_interval=0.0,
        context_bound=True,
        task_id="task-context-bound",
    )

    assert [event_name for _session_key, event_name, _payload in emitted] == [
        "session.event.answer_generation_reset",
        "session.event.done",
    ]
    assert not any(event_name == "session.event.error" for _, event_name, _ in emitted)


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
