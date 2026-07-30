from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.engine.types import ErrorEvent
from opensquilla.gateway.boot import (
    TaskRuntimeStreamError,
    _emit_task_runtime_stream_events,
    dispatch_task_runtime_turn,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import SubagentCompletionEvent, TaskRuntime
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus


def _make_envelope(
    session_key: str = "agent-1::sess-1",
    *,
    metadata: dict[str, Any] | None = None,
    input_provenance: dict[str, Any] | None = None,
) -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id="agent-1",
        session_key=session_key,
        input_provenance=input_provenance or {"kind": "test"},
        metadata=metadata or {},
    )


def _make_storage() -> Any:
    storage = MagicMock()
    task_db: dict[str, AgentTaskRecord] = {}

    async def create(record: AgentTaskRecord) -> None:
        task_db[record.task_id] = record

    async def update(task_id: str, **kwargs: Any) -> None:
        rec = task_db.get(task_id)
        if rec is None:
            return
        for key, value in kwargs.items():
            if hasattr(rec, key):
                object.__setattr__(rec, key, value)

    async def get(task_id: str) -> AgentTaskRecord | None:
        return task_db.get(task_id)

    storage.create_agent_task = create
    storage.update_agent_task = update
    storage.get_agent_task = get
    return storage


def _make_runtime(
    turn_handler: Callable[..., Awaitable[Any]],
    *,
    event_emitter: Callable[[str, str, dict[str, Any]], Awaitable[None]] | None = None,
    terminal_listener: Callable[[SubagentCompletionEvent], Awaitable[None]] | None = None,
    storage: Any | None = None,
) -> TaskRuntime:
    return TaskRuntime(
        storage=storage or _make_storage(),
        turn_handler=turn_handler,
        event_emitter=event_emitter,
        terminal_listener=terminal_listener,
    )


@pytest.mark.asyncio
async def test_mark_terminal_emits_additive_terminal_message_for_timeout_payload() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    async def _timeout_handler(_run: Any) -> None:
        raise TimeoutError("Gateway task timeout: Stream idle for more than 60s")

    runtime = _make_runtime(_timeout_handler, event_emitter=_emitter)
    handle = await runtime.enqueue(_make_envelope(), "hello")

    record = await runtime.wait(handle.task_id, timeout=2.0)

    terminal_events = [event for event in emitted if event[1] == "task.timeout"]
    assert len(terminal_events) == 1
    payload = terminal_events[0][2]
    assert payload["task_id"] == handle.task_id
    assert payload["terminal_reason"] == "timeout"
    assert payload["terminal_message"]
    assert "timed out" in payload["terminal_message"].lower()
    assert "Gateway task timeout" not in payload["terminal_message"]
    assert "Stream idle for more than" not in payload["terminal_message"]
    assert record.terminal_reason == "timeout"
    assert record.error_class == "TimeoutError"
    assert record.error_message == "The task timed out before it could finish."
    assert record.details is not None
    assert record.details["turn_outcome"]["kind"] == "interrupted"
    assert record.details["turn_outcome"]["error_class"] == "TimeoutError"


@pytest.mark.asyncio
async def test_terminal_event_still_emits_when_terminal_persistence_is_locked() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []
    storage = _make_storage()
    base_update = storage.update_agent_task

    async def _locked_terminal_update(task_id: str, **kwargs: Any) -> None:
        if kwargs.get("finished_at") is not None:
            raise sqlite3.OperationalError("database is locked")
        await base_update(task_id, **kwargs)

    storage.update_agent_task = _locked_terminal_update

    async def _failing_handler(_run: Any) -> None:
        raise RuntimeError("boom")

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    runtime = _make_runtime(_failing_handler, event_emitter=_emitter, storage=storage)
    handle = await runtime.enqueue(_make_envelope(), "hello")

    record = await runtime.wait(handle.task_id, timeout=2.0)

    assert record.status == AgentTaskStatus.FAILED
    assert record.terminal_reason == "error"
    terminal_events = [event for event in emitted if event[1] == "task.failed"]
    assert len(terminal_events) == 1
    payload = terminal_events[0][2]
    assert payload["task_id"] == handle.task_id
    assert payload["terminal_reason"] == "error"
    assert "failed" in payload["terminal_message"].lower()


@pytest.mark.asyncio
async def test_cancelled_task_persists_cancel_source_details() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_handler(_run: Any) -> None:
        started.set()
        await release.wait()

    runtime = _make_runtime(_blocking_handler)
    handle = await runtime.enqueue(_make_envelope(), "hello")
    await asyncio.wait_for(started.wait(), timeout=2.0)

    cancelled = await runtime.cancel(
        task_id=handle.task_id,
        source="webui_escape",
        reason="user_abort",
    )
    record = await runtime.wait(handle.task_id, timeout=2.0)

    assert cancelled == 1
    assert record.status == AgentTaskStatus.CANCELLED
    assert record.terminal_reason == "cancelled"
    assert record.details is not None
    assert record.details["cancellation"] == {
        "source": "webui_escape",
        "reason": "user_abort",
    }
    assert record.details["turn_outcome"]["kind"] == "interrupted"
    assert record.details["turn_outcome"]["reason"] == "cancelled"
    assert record.details["turn_outcome"]["cancellation_source"] == "webui_escape"


@pytest.mark.asyncio
async def test_context_overflow_failure_is_sanitized_in_record_and_subagent_event() -> None:
    raw_error = (
        "Context overflow is in the current turn's recent tool calls or "
        "reasoning tail; history compaction cannot reduce it."
    )
    terminal_events: list[SubagentCompletionEvent] = []

    async def _listener(event: SubagentCompletionEvent) -> None:
        terminal_events.append(event)

    async def _overflow_handler(_run: Any) -> None:
        raise RuntimeError(raw_error)

    runtime = _make_runtime(_overflow_handler, terminal_listener=_listener)
    handle = await runtime.enqueue(
        _make_envelope(
            session_key="agent:worker:subagent:overflow",
            metadata={
                "parent_session_key": "agent:main:webchat:parent",
                "parent_task_id": "parent-task",
            },
        ),
        "summarize a very large result",
        run_kind="subagent",
    )

    record = await runtime.wait(handle.task_id, timeout=2.0)

    assert record.status == AgentTaskStatus.FAILED
    assert record.error_class == "provider_request_too_large"
    assert record.error_message is not None
    assert "too large" in record.error_message.lower()
    assert raw_error not in record.error_message
    assert "history compaction cannot reduce it" not in record.error_message
    assert record.details is not None
    assert record.details["turn_outcome"]["kind"] == "budgetLimited"
    assert record.details["turn_outcome"]["reason"] == "provider_request_too_large"
    assert terminal_events
    event_payload = terminal_events[-1].to_payload()
    assert event_payload["error_class"] == "provider_request_too_large"
    assert "too large" in event_payload["error_message"].lower()
    assert raw_error not in event_payload["error_message"]


@pytest.mark.asyncio
async def test_successful_parent_task_persists_subagent_group_outcome_details() -> None:
    outcome = {
        "total": 2,
        "succeeded": 1,
        "failed": 1,
        "timeout": 0,
        "cancelled": 0,
        "abandoned": 0,
        "non_success": 1,
        "failed_children": [
            {
                "child_session_key": "agent:worker:subagent:failed",
                "task_id": "task-failed",
                "status": "failed",
                "terminal_reason": "tool_error",
                "error_class": "RuntimeError",
                "error_message": "boom",
            }
        ],
    }

    async def _success_handler(run: Any) -> None:
        assert run.input_provenance["subagent_group_outcome"] == outcome

    runtime = _make_runtime(_success_handler)
    handle = await runtime.enqueue(
        _make_envelope(
            input_provenance={
                "kind": "internal_system",
                "source_tool": "subagent_completion",
                "runtime_partial_failure_disclosure_required": True,
                "subagent_group_outcome": outcome,
            },
            metadata={"existing": "metadata"},
        ),
        "synthesize",
    )

    record = await runtime.wait(handle.task_id, timeout=2.0)

    assert record.status == AgentTaskStatus.SUCCEEDED
    assert record.details is not None
    assert record.details["source_name"] == "test"
    assert record.details["metadata"] == {"existing": "metadata"}
    assert record.details["input_provenance"]["source_tool"] == "subagent_completion"
    assert record.details["subagent_group_outcome"] == outcome
    assert record.details["turn_outcome"]["kind"] == "completed"


def test_subagent_completion_payload_adds_terminal_message_for_non_success() -> None:
    event = SubagentCompletionEvent(
        parent_session_key="agent:main:parent",
        child_session_key="agent:worker:child",
        task_id="task-child",
        status=AgentTaskStatus.FAILED,
        terminal_reason="error",
        error_class="RuntimeError",
        error_message="boom",
    )

    payload = event.to_payload()

    assert payload["terminal_reason"] == "error"
    assert payload["error_class"] == "RuntimeError"
    assert payload["error_message"] == "boom"
    assert payload["terminal_message"]
    assert "failed" in payload["terminal_message"].lower()


def test_subagent_completion_payload_keeps_success_payload_unchanged() -> None:
    event = SubagentCompletionEvent(
        parent_session_key="agent:main:parent",
        child_session_key="agent:worker:child",
        task_id="task-child",
        status=AgentTaskStatus.SUCCEEDED,
        terminal_reason="completed",
    )

    assert "terminal_message" not in event.to_payload()


@pytest.mark.asyncio
async def test_task_runtime_stream_error_emits_sanitized_terminal_message() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _stream():
        yield ErrorEvent(
            message="Iteration 1 exceeded iteration_timeout",
            code="iteration_timeout",
        )

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    with pytest.raises(RuntimeError, match="The task timed out before it could finish"):
        await _emit_task_runtime_stream_events(
            _stream(),
            "agent:main:test",
            _emitter,
            stream_event_sink=None,
            idle_timeout=1.0,
            heartbeat_interval=0.0,
        )

    assert emitted == [
        (
            "agent:main:test",
            "session.event.error",
            {
                "message": "The task timed out before it could finish.",
                "code": "iteration_timeout",
                # Additive wire field: durable turn_errors reference id
                # (empty when no record was written for this error).
                "error_id": "",
                "terminal_message": "The task timed out before it could finish.",
                "terminal_reason": "timeout",
                "error_message": "The task timed out before it could finish.",
            },
        )
    ]


@pytest.mark.asyncio
async def test_task_runtime_stream_error_terminal_message_carries_error_ref() -> None:
    # Additive: when the turn loop stamped an error_id, the stream emitter
    # suffixes the user-facing text with the durable turn_errors reference.
    # An empty error_id (test above) must leave the text untouched.
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _stream():
        yield ErrorEvent(message="Agent error", code="agent_error", error_id="abcd1234")

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    with pytest.raises(TaskRuntimeStreamError):
        await _emit_task_runtime_stream_events(
            _stream(),
            "agent:main:test",
            _emitter,
            stream_event_sink=None,
            idle_timeout=1.0,
            heartbeat_interval=0.0,
        )

    payload = emitted[-1][2]
    assert payload["error_id"] == "abcd1234"
    assert payload["message"].endswith("(ref: abcd1234)")
    assert payload["terminal_message"].endswith("(ref: abcd1234)")


@pytest.mark.asyncio
async def test_task_runtime_stream_output_truncation_is_terminal_state() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _stream():
        yield ErrorEvent(
            message="Provider output limit reached before completion",
            code="provider_output_truncated",
        )

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    with pytest.raises(TaskRuntimeStreamError) as exc_info:
        await _emit_task_runtime_stream_events(
            _stream(),
            "agent:main:test",
            _emitter,
            stream_event_sink=None,
            idle_timeout=1.0,
            heartbeat_interval=0.0,
        )

    assert exc_info.value.code == "provider_output_truncated"
    assert exc_info.value.terminal_reason == "output_truncated"
    payload = emitted[-1][2]
    assert payload["code"] == "provider_output_truncated"
    assert payload["terminal_reason"] == "output_truncated"
    assert "output limit" in payload["terminal_message"].lower()
    assert payload["error_message"] == (
        "The provider stopped because the output limit was reached before the task finished."
    )
    assert "Provider output limit reached before completion" not in payload["error_message"]


@pytest.mark.asyncio
async def test_task_runtime_records_output_truncation_as_failed_not_succeeded() -> None:
    async def _truncated_handler(_run: Any) -> None:
        raise TaskRuntimeStreamError(
            "Provider output limit reached before completion",
            code="provider_output_truncated",
            terminal_reason="output_truncated",
        )

    runtime = _make_runtime(_truncated_handler)
    handle = await runtime.enqueue(_make_envelope(), "make a deck")

    record = await runtime.wait(handle.task_id, timeout=2.0)

    assert record.status == AgentTaskStatus.FAILED
    assert record.terminal_reason == "output_truncated"
    assert record.error_class == "provider_output_truncated"
    assert record.error_message == (
        "The provider stopped because the output limit was reached before the task finished."
    )
    assert "Provider output limit reached before completion" not in record.error_message


@pytest.mark.asyncio
async def test_task_runtime_records_stream_timeout_reason_as_timeout() -> None:
    async def _timeout_handler(_run: Any) -> None:
        raise TaskRuntimeStreamError(
            "Iteration 1 exceeded iteration_timeout",
            code="iteration_timeout",
            terminal_reason="timeout",
        )

    runtime = _make_runtime(_timeout_handler)
    handle = await runtime.enqueue(_make_envelope(), "hello")

    record = await runtime.wait(handle.task_id, timeout=2.0)

    assert record.status == AgentTaskStatus.TIMEOUT
    assert record.terminal_reason == "timeout"
    assert record.error_class == "iteration_timeout"
    assert record.error_message == "The task timed out before it could finish."


@pytest.mark.asyncio
async def test_task_runtime_stream_context_overflow_hides_raw_agent_error() -> None:
    raw_error = (
        "Context overflow is in the current turn's recent tool calls or "
        "reasoning tail; history compaction cannot reduce it."
    )
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _stream():
        yield ErrorEvent(message=raw_error, code="current_turn_context_exhausted")

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    with pytest.raises(TaskRuntimeStreamError) as exc_info:
        await _emit_task_runtime_stream_events(
            _stream(),
            "agent:main:test",
            _emitter,
            stream_event_sink=None,
            idle_timeout=1.0,
            heartbeat_interval=0.0,
        )

    assert exc_info.value.code == "provider_request_too_large"
    assert raw_error not in str(exc_info.value)
    assert "current_turn_context_exhausted" not in str(exc_info.value)
    payload = emitted[-1][2]
    assert payload["code"] == "provider_request_too_large"
    assert "too large" in payload["message"].lower()
    assert "too large" in payload["error_message"].lower()
    assert raw_error not in payload["error_message"]
    assert "current_turn_context_exhausted" not in payload["error_message"]


@pytest.mark.asyncio
async def test_task_runtime_rolls_back_persisted_user_on_provider_budget_error() -> None:
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    class RecordingSessionManager:
        def __init__(self) -> None:
            self.removed: list[tuple[str, str]] = []
            self.rollback_lock: asyncio.Lock | None = None
            self.rollback_lock_states: list[bool] = []

        async def get_session(self, session_key: str) -> Any:  # noqa: ARG002
            return None

        async def remove_message(self, session_key: str, message_id: str) -> bool:
            self.rollback_lock_states.append(
                self.rollback_lock is not None and self.rollback_lock.locked()
            )
            self.removed.append((session_key, message_id))
            return True

    class ProviderBudgetErrorRunner:
        def __init__(self) -> None:
            self.lock = asyncio.Lock()

        def _get_session_lock(self, session_key: str) -> asyncio.Lock:  # noqa: ARG002
            return self.lock

        async def run(self, message: str, session_key: str, **kwargs: Any):  # noqa: ARG002
            yield ErrorEvent(
                message='{"fallback_reason":"provider_request_budget_exhausted"}',
                code="provider_request_budget_exhausted",
            )

    async def _emitter(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        emitted.append((session_key, event_name, payload))

    manager = RecordingSessionManager()
    runner = ProviderBudgetErrorRunner()
    manager.rollback_lock = runner.lock
    run = SimpleNamespace(
        agent_id="main",
        task_id="task-1",
        session_key="agent:main:test",
        message="large paste",
        envelope=_make_envelope("agent:main:test"),
        attachments=[],
        input_provenance={},
        run_kind="interactive",
        no_memory_capture=False,
        ingress_pipeline_steps=[],
        semantic_message=None,
        persisted_user_message_id="msg-1",
        persisted_user_message_ids=("msg-1", "msg-2", "msg-3"),
        stream_event_sink=None,
    )

    with pytest.raises(TaskRuntimeStreamError) as exc_info:
        await dispatch_task_runtime_turn(
            run,
            config=GatewayConfig(
                agent_stream_heartbeat_interval_seconds=0.0,
                agent_stream_idle_timeout_seconds=1.0,
            ),
            session_manager=manager,
            turn_runner=runner,
            event_emitter=_emitter,
        )

    assert exc_info.value.code == "provider_request_too_large"
    assert manager.removed == [
        ("agent:main:test", "msg-1"),
        ("agent:main:test", "msg-2"),
        ("agent:main:test", "msg-3"),
    ]
    assert manager.rollback_lock_states == [True, True, True]
    payload = emitted[0][2]
    assert payload["code"] == "provider_request_too_large"
    assert "too large" in payload["terminal_message"]
    assert "automatic context compaction" in payload["terminal_message"]
    assert "send less text" not in payload["terminal_message"]
    assert "failed before" not in payload["terminal_message"]
