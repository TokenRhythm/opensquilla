from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.types import DoneEvent, TextDeltaEvent
from opensquilla.gateway.background_completion import (
    BackgroundCompletionManager,
    _SynthesisStreamCollector,
)
from opensquilla.gateway.boot import GatewayServer
from opensquilla.gateway.routing import ReplyTarget, RouteEnvelope, SourceKind
from opensquilla.session.models import AgentTaskStatus

PARENT = "agent:main:channel:parent"
PARENT_TASK = "task-parent"


@pytest.mark.asyncio
async def test_synthesis_collector_prefers_present_terminal_snapshot() -> None:
    collector = _SynthesisStreamCollector()

    await collector(TextDeltaEvent(text="stale"))
    await collector(DoneEvent(text="canonical", text_snapshot="canonical"))

    assert collector.text() == "canonical"


@pytest.mark.asyncio
async def test_synthesis_collector_distinguishes_empty_snapshot_from_legacy_fallback() -> None:
    explicit_empty = _SynthesisStreamCollector()
    await explicit_empty(TextDeltaEvent(text="stale"))
    await explicit_empty(DoneEvent(text="", text_snapshot=""))

    legacy_partial = _SynthesisStreamCollector()
    await legacy_partial(TextDeltaEvent(text="partial"))
    await legacy_partial(DoneEvent())

    assert explicit_empty.text() == ""
    assert legacy_partial.text() == "partial"


class _SessionManager:
    def __init__(self, *, parent: Any | None = None, transcript: list[Any] | None = None) -> None:
        self.parent = parent
        self.transcript = list(transcript or [])

    async def read_transcript(self, session_key: str):
        return list(self.transcript)

    async def get_session(self, session_key: str):
        return self.parent


class _Adapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        if self.fail:
            raise RuntimeError("channel down")
        self.sent.append(message)


class _ChannelManager:
    def __init__(self, adapter: _Adapter | None) -> None:
        self.adapter = adapter
        self.requested_names: list[str] = []

    def get(self, channel_name: str):
        self.requested_names.append(channel_name)
        return self.adapter


class _TaskRuntime:
    def __init__(self) -> None:
        self.parent_released = asyncio.Event()
        self.synthesis_released = asyncio.Event()
        self.synthesis_status = AgentTaskStatus.SUCCEEDED
        self.sent: list[tuple[str, str, dict[str, Any] | None]] = []
        self.sent_envelopes: list[Any] = []
        self.stream_event_sink = None
        self._tasks: dict[str, Any] = {}

    async def send(
        self,
        session_key: str,
        message: str,
        provenance: dict[str, Any] | None = None,
        stream_event_sink=None,
    ):
        self.sent.append((session_key, message, provenance))
        self.stream_event_sink = stream_event_sink
        return SimpleNamespace(task_id="task-synthesis")

    async def send_with_envelope(
        self,
        envelope: Any,
        message: str,
        provenance: dict[str, Any] | None = None,
        stream_event_sink=None,
    ):
        self.sent_envelopes.append(envelope)
        return await self.send(
            envelope.session_key,
            message,
            provenance=provenance,
            stream_event_sink=stream_event_sink,
        )

    async def wait(self, task_id: str):
        if task_id == PARENT_TASK:
            await self.parent_released.wait()
            return SimpleNamespace(task_id=task_id, status=AgentTaskStatus.SUCCEEDED)
        if task_id == "task-synthesis":
            await self.synthesis_released.wait()
            return SimpleNamespace(task_id=task_id, status=self.synthesis_status)
        raise KeyError(task_id)

    async def emit_text_delta(self, text: str) -> None:
        assert self.stream_event_sink is not None
        await self.stream_event_sink(TextDeltaEvent(text=text))

    async def emit_done_text(self, text: str) -> None:
        assert self.stream_event_sink is not None
        await self.stream_event_sink(DoneEvent(text=text))


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_parent_wake_waits_out_of_band_and_delivers_final_channel_text() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    runtime = _TaskRuntime()
    adapter = _Adapter()
    session_manager = _SessionManager(
        parent=SimpleNamespace(last_channel="slack", last_to="C123", last_thread_id="T456"),
        transcript=[SimpleNamespace(role="assistant", content="yield placeholder")],
    )
    manager = BackgroundCompletionManager(
        session_manager=session_manager,
        event_emitter=lambda _session, event, payload: _record(events, event, payload),
        channel_manager_ref=lambda: _ChannelManager(adapter),
    )

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[{"child_session_key": "child"}],
        task_runtime=runtime,
        message="wake",
        provenance={"kind": "internal_system"},
    )

    assert runtime.sent == []
    runtime.parent_released.set()
    await _wait_until(lambda: len(runtime.sent) == 1)
    await runtime.emit_text_delta("final answer")
    runtime.synthesis_released.set()
    await _wait_until(lambda: any(event == "session.event.task_group.done" for event, _ in events))

    assert adapter.sent[0].content == "final answer"
    assert adapter.sent[0].reply_to == "T456"
    assert adapter.sent[0].metadata == {"channel": "C123"}
    assert [event for event, _ in events] == [
        "session.event.task_group.synthesizing",
        "session.event.task_group.done",
    ]
    done = events[-1][1]
    assert done["group_id"] == f"subagent:{PARENT}:{PARENT_TASK}"
    assert done["parent_session_key"] == PARENT
    assert done["parent_task_id"] == PARENT_TASK
    assert done["synthesis_task_id"] == "task-synthesis"
    assert done["delivery_status"] == "sent"


@pytest.mark.asyncio
async def test_parent_wake_preserves_captured_full_host_envelope_after_parent_finishes() -> None:
    runtime = _TaskRuntime()
    envelope = RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="webchat",
        agent_id="main",
        session_key=PARENT,
        metadata={
            "principal_is_owner": True,
            "run_mode": "full",
            "elevated": "full",
            "sandbox_run_context": {"run_mode": "full"},
        },
    )
    runtime._tasks[PARENT_TASK] = SimpleNamespace(envelope=envelope)
    manager = BackgroundCompletionManager(
        session_manager=_SessionManager(parent=SimpleNamespace(last_channel=None, last_to=None)),
    )

    await manager.capture_delivery_target(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        task_runtime=runtime,
    )
    runtime._tasks.clear()
    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[{"child_session_key": "child"}],
        task_runtime=runtime,
        message="wake",
        provenance={"kind": "internal_system"},
    )
    runtime.parent_released.set()
    await _wait_until(lambda: len(runtime.sent) == 1)

    assert runtime.sent_envelopes == [envelope]
    assert runtime.sent_envelopes[0].metadata["run_mode"] == "full"

    runtime.synthesis_released.set()
    await manager.drain(timeout=1.0)


@pytest.mark.asyncio
async def test_cancel_session_blocks_late_subagent_parent_wake() -> None:
    runtime = _TaskRuntime()
    manager = BackgroundCompletionManager(
        session_manager=_SessionManager(parent=SimpleNamespace(last_channel=None, last_to=None)),
    )

    await manager.emit_waiting(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        pending_count=1,
    )
    assert await manager.active_group_ids(PARENT) == [
        f"subagent:{PARENT}:{PARENT_TASK}"
    ]
    cancelled = await manager.cancel_session(PARENT)
    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[{"child_session_key": "child"}],
        task_runtime=runtime,
        message="wake",
        provenance={"kind": "internal_system"},
    )
    runtime.parent_released.set()
    await asyncio.sleep(0)

    assert cancelled == 1
    assert await manager.active_group_ids(PARENT) == []
    assert runtime.sent == []


@pytest.mark.asyncio
async def test_synthesis_done_text_delivers_when_no_text_delta_emitted() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    runtime = _TaskRuntime()
    adapter = _Adapter()
    session_manager = _SessionManager(
        parent=SimpleNamespace(last_channel="slack", last_to="C123", last_thread_id="T456"),
        transcript=[
            SimpleNamespace(role="assistant", content="yield placeholder"),
            SimpleNamespace(role="assistant", content="unrelated transcript text"),
        ],
    )
    manager = BackgroundCompletionManager(
        session_manager=session_manager,
        event_emitter=lambda _session, event, payload: _record(events, event, payload),
        channel_manager_ref=lambda: _ChannelManager(adapter),
    )

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[{"child_session_key": "child"}],
        task_runtime=runtime,
        message="wake",
        provenance={"kind": "internal_system"},
    )
    runtime.parent_released.set()
    await _wait_until(lambda: len(runtime.sent) == 1)
    await runtime.emit_done_text("done-only final answer")
    runtime.synthesis_released.set()
    await _wait_until(lambda: any(event == "session.event.task_group.done" for event, _ in events))

    assert adapter.sent[0].content == "done-only final answer"
    assert events[-1][1]["delivery_status"] == "sent"


@pytest.mark.asyncio
async def test_parent_wake_uses_parent_task_route_when_session_route_changes() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    runtime = _TaskRuntime()
    runtime._tasks[PARENT_TASK] = SimpleNamespace(
        envelope=RouteEnvelope(
            source_kind=SourceKind.CHANNEL,
            source_name="slack",
            agent_id="main",
            session_key=PARENT,
            channel_name="slack",
            channel_id="C-old",
            thread_id="T-old",
            reply_target=ReplyTarget(
                kind="channel",
                channel_name="slack",
                to="C-old",
                thread_id="T-old",
            ),
        )
    )
    adapter = _Adapter()
    channel_manager = _ChannelManager(adapter)
    session_manager = _SessionManager(
        parent=SimpleNamespace(last_channel="slack", last_to="C-new", last_thread_id="T-new"),
    )
    manager = BackgroundCompletionManager(
        session_manager=session_manager,
        event_emitter=lambda _session, event, payload: _record(events, event, payload),
        channel_manager_ref=lambda: channel_manager,
    )

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[{"child_session_key": "child"}],
        task_runtime=runtime,
        message="wake",
        provenance={"kind": "internal_system"},
    )
    runtime.parent_released.set()
    await _wait_until(lambda: len(runtime.sent) == 1)
    await runtime.emit_text_delta("final answer")
    runtime.synthesis_released.set()
    await _wait_until(lambda: any(event == "session.event.task_group.done" for event, _ in events))

    assert channel_manager.requested_names == ["slack"]
    assert adapter.sent[0].content == "final answer"
    assert adapter.sent[0].reply_to == "T-old"
    assert adapter.sent[0].metadata == {"channel": "C-old"}


@pytest.mark.asyncio
async def test_parent_wake_freezes_session_route_before_synthesis_finishes() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    runtime = _TaskRuntime()
    adapter = _Adapter()
    session_manager = _SessionManager(
        parent=SimpleNamespace(last_channel="slack", last_to="C-old", last_thread_id="T-old"),
    )
    manager = BackgroundCompletionManager(
        session_manager=session_manager,
        event_emitter=lambda _session, event, payload: _record(events, event, payload),
        channel_manager_ref=lambda: _ChannelManager(adapter),
    )

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[{"child_session_key": "child"}],
        task_runtime=runtime,
        message="wake",
        provenance={"kind": "internal_system"},
    )
    session_manager.parent.last_to = "C-new"
    session_manager.parent.last_thread_id = "T-new"
    runtime.parent_released.set()
    await _wait_until(lambda: len(runtime.sent) == 1)
    await runtime.emit_text_delta("final answer")
    runtime.synthesis_released.set()
    await _wait_until(lambda: any(event == "session.event.task_group.done" for event, _ in events))

    assert adapter.sent[0].reply_to == "T-old"
    assert adapter.sent[0].metadata == {"channel": "C-old"}


@pytest.mark.asyncio
async def test_parent_wake_uses_target_captured_before_parent_task_eviction() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    runtime = _TaskRuntime()
    runtime._tasks[PARENT_TASK] = SimpleNamespace(
        envelope=RouteEnvelope(
            source_kind=SourceKind.CHANNEL,
            source_name="slack",
            agent_id="main",
            session_key=PARENT,
            channel_name="slack",
            channel_id="C-old",
            thread_id="T-old",
            reply_target=ReplyTarget(
                kind="channel",
                channel_name="slack",
                to="C-old",
                thread_id="T-old",
            ),
        )
    )
    adapter = _Adapter()
    session_manager = _SessionManager(
        parent=SimpleNamespace(
            last_channel="slack",
            last_to="C-original",
            last_thread_id="T-original",
        ),
    )
    manager = BackgroundCompletionManager(
        session_manager=session_manager,
        event_emitter=lambda _session, event, payload: _record(events, event, payload),
        channel_manager_ref=lambda: _ChannelManager(adapter),
    )

    await manager.capture_delivery_target(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        task_runtime=runtime,
    )
    runtime._tasks.clear()
    session_manager.parent.last_to = "C-new"
    session_manager.parent.last_thread_id = "T-new"

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[{"child_session_key": "child"}],
        task_runtime=runtime,
        message="wake",
        provenance={"kind": "internal_system"},
    )
    runtime.parent_released.set()
    await _wait_until(lambda: len(runtime.sent) == 1)
    await runtime.emit_text_delta("final answer")
    runtime.synthesis_released.set()
    await _wait_until(lambda: any(event == "session.event.task_group.done" for event, _ in events))

    assert adapter.sent[0].reply_to == "T-old"
    assert adapter.sent[0].metadata == {"channel": "C-old"}


@pytest.mark.asyncio
async def test_unrelated_post_watermark_assistant_text_is_not_applicable() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    runtime = _TaskRuntime()
    adapter = _Adapter()
    session_manager = _SessionManager(
        parent=SimpleNamespace(last_channel="slack", last_to="C123", last_thread_id="T456"),
        transcript=[SimpleNamespace(role="assistant", content="yield placeholder")],
    )
    manager = BackgroundCompletionManager(
        session_manager=session_manager,
        event_emitter=lambda _session, event, payload: _record(events, event, payload),
        channel_manager_ref=lambda: _ChannelManager(adapter),
    )

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[{"child_session_key": "child"}],
        task_runtime=runtime,
        message="wake",
        provenance={"kind": "internal_system"},
    )
    runtime.parent_released.set()
    await _wait_until(lambda: len(runtime.sent) == 1)
    session_manager.transcript.append(SimpleNamespace(role="assistant", content="unrelated answer"))
    runtime.synthesis_released.set()
    await _wait_until(lambda: any(event == "session.event.task_group.done" for event, _ in events))

    assert adapter.sent == []
    assert events[-1][1]["delivery_status"] == "not_applicable"


@pytest.mark.asyncio
async def test_channel_delivery_failure_is_reported_without_failing_group() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    runtime = _TaskRuntime()
    session_manager = _SessionManager(
        parent=SimpleNamespace(last_channel="slack", last_to="C123", last_thread_id="T456"),
        transcript=[SimpleNamespace(role="assistant", content="yield placeholder")],
    )
    manager = BackgroundCompletionManager(
        session_manager=session_manager,
        event_emitter=lambda _session, event, payload: _record(events, event, payload),
        channel_manager_ref=lambda: _ChannelManager(_Adapter(fail=True)),
    )

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[{"child_session_key": "child"}],
        task_runtime=runtime,
        message="wake",
        provenance={"kind": "internal_system"},
    )
    runtime.parent_released.set()
    await _wait_until(lambda: len(runtime.sent) == 1)
    await runtime.emit_text_delta("final answer")
    runtime.synthesis_released.set()
    await _wait_until(lambda: any(event == "session.event.task_group.done" for event, _ in events))

    assert events[-1][0] == "session.event.task_group.done"
    assert events[-1][1]["delivery_status"] == "failed"
    assert events[-1][1]["delivery_error_class"] == "RuntimeError"


@pytest.mark.asyncio
async def test_synthesis_failure_emits_group_failed() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    runtime = _TaskRuntime()
    runtime.synthesis_status = AgentTaskStatus.FAILED
    session_manager = _SessionManager(parent=SimpleNamespace(last_channel=None, last_to=None))
    manager = BackgroundCompletionManager(
        session_manager=session_manager,
        event_emitter=lambda _session, event, payload: _record(events, event, payload),
        channel_manager_ref=lambda: _ChannelManager(None),
    )

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[{"child_session_key": "child"}],
        task_runtime=runtime,
        message="wake",
        provenance={"kind": "internal_system"},
    )
    runtime.parent_released.set()
    await _wait_until(lambda: len(runtime.sent) == 1)
    runtime.synthesis_released.set()
    await _wait_until(
        lambda: any(event == "session.event.task_group.failed" for event, _ in events)
    )

    failed = events[-1][1]
    assert failed["status"] == "failed"
    assert failed["synthesis_status"] == "failed"
    assert failed["delivery_status"] == "not_applicable"


@pytest.mark.asyncio
async def test_waiting_event_is_not_reemitted_after_wake_starts() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    runtime = _TaskRuntime()
    manager = BackgroundCompletionManager(
        session_manager=_SessionManager(parent=SimpleNamespace(last_channel=None, last_to=None)),
        event_emitter=lambda _session, event, payload: _record(events, event, payload),
        channel_manager_ref=lambda: _ChannelManager(None),
    )

    await manager.emit_waiting(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        pending_count=0,
    )
    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[{"child_session_key": "child"}],
        task_runtime=runtime,
        message="wake",
        provenance={"kind": "internal_system"},
    )
    await manager.emit_waiting(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        pending_count=0,
    )

    assert [event for event, _ in events] == ["session.event.task_group.waiting"]


@pytest.mark.asyncio
async def test_background_completion_drain_waits_for_detached_watcher() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    runtime = _TaskRuntime()
    adapter = _Adapter()
    manager = BackgroundCompletionManager(
        session_manager=_SessionManager(
            parent=SimpleNamespace(last_channel="slack", last_to="C123", last_thread_id="T456")
        ),
        event_emitter=lambda _session, event, payload: _record(events, event, payload),
        channel_manager_ref=lambda: _ChannelManager(adapter),
    )

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[{"child_session_key": "child"}],
        task_runtime=runtime,
        message="wake",
        provenance={"kind": "internal_system"},
    )
    drain_task = asyncio.create_task(manager.drain(timeout=1.0))
    await asyncio.sleep(0)
    assert not drain_task.done()

    runtime.parent_released.set()
    await _wait_until(lambda: len(runtime.sent) == 1)
    await runtime.emit_text_delta("final answer")
    runtime.synthesis_released.set()
    await drain_task

    assert adapter.sent[0].content == "final answer"
    assert events[-1][0] == "session.event.task_group.done"


@pytest.mark.asyncio
async def test_background_completion_drain_timeout_does_not_cancel_watcher() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    runtime = _TaskRuntime()
    adapter = _Adapter()
    manager = BackgroundCompletionManager(
        session_manager=_SessionManager(
            parent=SimpleNamespace(last_channel="slack", last_to="C123", last_thread_id="T456")
        ),
        event_emitter=lambda _session, event, payload: _record(events, event, payload),
        channel_manager_ref=lambda: _ChannelManager(adapter),
    )

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[{"child_session_key": "child"}],
        task_runtime=runtime,
        message="wake",
        provenance={"kind": "internal_system"},
    )
    await manager.drain(timeout=0.01)

    runtime.parent_released.set()
    await _wait_until(lambda: len(runtime.sent) == 1)
    await runtime.emit_text_delta("final answer")
    runtime.synthesis_released.set()
    await _wait_until(lambda: any(event == "session.event.task_group.done" for event, _ in events))

    assert adapter.sent[0].content == "final answer"


@pytest.mark.asyncio
async def test_background_completion_close_rejects_new_wake_registration() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    runtime = _TaskRuntime()
    manager = BackgroundCompletionManager(
        session_manager=_SessionManager(
            parent=SimpleNamespace(last_channel="slack", last_to="C123", last_thread_id="T456")
        ),
        event_emitter=lambda _session, event, payload: _record(events, event, payload),
        channel_manager_ref=lambda: _ChannelManager(_Adapter()),
    )

    await manager.close(timeout=0.1)
    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[{"child_session_key": "child"}],
        task_runtime=runtime,
        message="wake",
        provenance={"kind": "internal_system"},
    )
    await asyncio.sleep(0)

    assert runtime.sent == []
    assert events == []


@pytest.mark.asyncio
async def test_gateway_close_drains_background_completion_before_stopping_channels() -> None:
    order: list[str] = []

    class _Runtime:
        async def shutdown(self, **_kwargs) -> None:
            order.append("runtime")

    class _Services:
        task_runtime = _Runtime()

        async def close(self) -> None:
            order.append("services")

    class _Background:
        async def close(self, **_kwargs) -> None:
            order.append("background")

    class _Channels:
        async def stop_all(self) -> None:
            order.append("channels")

    server = GatewayServer(app=SimpleNamespace(), config=SimpleNamespace())
    server._services = _Services()
    server._background_completion_manager = _Background()
    server._channel_manager = _Channels()

    await server.close()

    assert order.index("runtime") < order.index("background") < order.index("channels")
    assert order[-1] == "services"


async def _record(
    events: list[tuple[str, dict[str, Any]]],
    event: str,
    payload: dict[str, Any],
) -> None:
    events.append((event, payload))
