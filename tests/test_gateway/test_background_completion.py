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
from opensquilla.gateway.project_workspace_runtime import AcceptedRunModeOverride
from opensquilla.gateway.routing import ReplyTarget, RouteEnvelope, SourceKind
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.session.models import AgentTaskStatus

PARENT = "agent:main:channel:parent"
PARENT_TASK = "task-parent"
OTHER_PARENT = "agent:main:channel:other-parent"


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
        self.sent_run_mode_overrides: list[Any] = []
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
        accepted_run_mode_override: Any | None = None,
    ):
        self.sent_envelopes.append(envelope)
        self.sent_run_mode_overrides.append(accepted_run_mode_override)
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


class _BlockingSendRuntime:
    def __init__(self) -> None:
        self.send_entered: dict[str, asyncio.Event] = {}
        self.send_release: dict[str, asyncio.Event] = {}
        self.send_cancelled: list[str] = []
        self.send_calls: list[str] = []
        self.sent: list[str] = []
        self.cancel_cleanup_release: dict[str, asyncio.Event] = {}

    def _event(self, events: dict[str, asyncio.Event], session_key: str) -> asyncio.Event:
        return events.setdefault(session_key, asyncio.Event())

    async def send(
        self,
        session_key: str,
        _message: str,
        **_kwargs: Any,
    ) -> Any:
        self.send_calls.append(session_key)
        self._event(self.send_entered, session_key).set()
        try:
            await self._event(self.send_release, session_key).wait()
        except asyncio.CancelledError:
            self.send_cancelled.append(session_key)
            cleanup_release = self.cancel_cleanup_release.get(session_key)
            if cleanup_release is not None:
                await cleanup_release.wait()
            raise
        self.sent.append(session_key)
        return SimpleNamespace(task_id=f"synthesis:{session_key}")

    async def wait(self, _task_id: str) -> Any:
        return SimpleNamespace(status=AgentTaskStatus.SUCCEEDED)


class _ReplacementRuntime:
    def __init__(self) -> None:
        self.send_count = 0
        self.synthesis_release: dict[str, asyncio.Event] = {}
        self.wait_cancelled: list[str] = []

    async def send(self, _session_key: str, _message: str, **_kwargs: Any) -> Any:
        self.send_count += 1
        task_id = f"synthesis-{self.send_count}"
        self.synthesis_release[task_id] = asyncio.Event()
        return SimpleNamespace(task_id=task_id)

    async def wait(self, task_id: str) -> Any:
        if not task_id.startswith("synthesis-"):
            return SimpleNamespace(status=AgentTaskStatus.SUCCEEDED)
        try:
            await self.synthesis_release[task_id].wait()
        except asyncio.CancelledError:
            self.wait_cancelled.append(task_id)
            raise
        return SimpleNamespace(status=AgentTaskStatus.SUCCEEDED)


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
    accepted_override = AcceptedRunModeOverride(
        run_mode=RunMode.FULL,
        run_mode_source="user",
        source="request",
    )
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
    runtime._tasks[PARENT_TASK] = SimpleNamespace(
        envelope=envelope,
        accepted_run_mode_override=accepted_override,
    )
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
    assert runtime.sent_run_mode_overrides == [accepted_override]

    runtime.synthesis_released.set()
    await manager.drain(timeout=1.0)


@pytest.mark.asyncio
async def test_active_run_mode_override_tracks_background_group_lifecycle() -> None:
    runtime = _TaskRuntime()
    accepted_override = AcceptedRunModeOverride(
        run_mode=RunMode.SAFE,
        run_mode_source="user",
        source="request",
    )
    runtime._tasks[PARENT_TASK] = SimpleNamespace(
        envelope=RouteEnvelope(
            source_kind=SourceKind.WEB,
            source_name="webchat",
            agent_id="main",
            session_key=PARENT,
        ),
        accepted_run_mode_override=accepted_override,
    )
    manager = BackgroundCompletionManager(
        session_manager=_SessionManager(parent=SimpleNamespace(last_channel=None)),
    )

    await manager.capture_delivery_target(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        task_runtime=runtime,
    )
    await manager.emit_waiting(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        pending_count=1,
    )

    assert await manager.active_run_mode_override(PARENT) is accepted_override

    await manager.cancel_session(PARENT)
    assert await manager.active_run_mode_override(PARENT) is None


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
    assert await manager.active_group_ids(PARENT) == [f"subagent:{PARENT}:{PARENT_TASK}"]
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
async def test_cancel_task_preserves_other_groups_in_same_parent_session() -> None:
    manager = BackgroundCompletionManager(session_manager=_SessionManager())
    other_task_id = "task-other"

    await manager.emit_waiting(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        pending_count=1,
    )
    await manager.emit_waiting(
        parent_session_key=PARENT,
        parent_task_id=other_task_id,
        pending_count=1,
    )

    assert await manager.cancel_task(PARENT, other_task_id) == 1
    assert await manager.active_group_ids(PARENT) == [
        manager.group_id(PARENT, PARENT_TASK)
    ]

    # Cancellation is also a fence for a not-yet-admitted exact task group.
    late_task_id = "task-late"
    assert await manager.cancel_task(PARENT, late_task_id) == 0
    await manager.emit_waiting(
        parent_session_key=PARENT,
        parent_task_id=late_task_id,
        pending_count=1,
    )
    assert await manager.active_group_ids(PARENT) == [
        manager.group_id(PARENT, PARENT_TASK)
    ]


@pytest.mark.asyncio
async def test_quiesce_sessions_cancels_only_target_watcher_and_fences_new_groups() -> None:
    runtime = _BlockingSendRuntime()
    manager = BackgroundCompletionManager(session_manager=_SessionManager())

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[],
        task_runtime=runtime,
        message="target wake",
        provenance={"kind": "internal_system"},
    )
    await manager.send_parent_wake(
        parent_session_key=OTHER_PARENT,
        parent_task_id="other-task",
        payloads=[],
        task_runtime=runtime,
        message="unrelated wake",
        provenance={"kind": "internal_system"},
    )
    await asyncio.wait_for(runtime._event(runtime.send_entered, PARENT).wait(), timeout=1)
    await asyncio.wait_for(runtime._event(runtime.send_entered, OTHER_PARENT).wait(), timeout=1)

    fence_entered = asyncio.Event()
    release_fence = asyncio.Event()

    async def _hold_fence() -> None:
        async with manager.quiesce_sessions([PARENT]):
            fence_entered.set()
            await release_fence.wait()

    fence_task = asyncio.create_task(_hold_fence())
    await asyncio.wait_for(fence_entered.wait(), timeout=1)

    assert runtime.send_cancelled == [PARENT]
    assert await manager.active_group_ids(PARENT) == []
    assert await manager.active_group_ids(OTHER_PARENT) == [f"subagent:{OTHER_PARENT}:other-task"]

    await manager.emit_waiting(
        parent_session_key=PARENT,
        parent_task_id="fenced-task",
    )
    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id="fenced-task",
        payloads=[],
        task_runtime=runtime,
        message="must stay suppressed",
        provenance={"kind": "internal_system"},
    )
    release_fence.set()
    await fence_task

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id="fenced-task",
        payloads=[],
        task_runtime=runtime,
        message="cancelled group must not revive",
        provenance={"kind": "internal_system"},
    )
    assert runtime.send_calls.count(PARENT) == 1
    assert runtime.sent == []

    runtime._event(runtime.send_release, OTHER_PARENT).set()
    await manager.drain(timeout=1)
    assert runtime.sent == [OTHER_PARENT]


@pytest.mark.asyncio
async def test_quiesce_sessions_does_not_cancel_prefix_related_parent() -> None:
    prefix_related_parent = f"{PARENT}:nested"
    runtime = _BlockingSendRuntime()
    manager = BackgroundCompletionManager(session_manager=_SessionManager())

    await manager.send_parent_wake(
        parent_session_key=prefix_related_parent,
        parent_task_id="prefix-related-task",
        payloads=[],
        task_runtime=runtime,
        message="unrelated wake",
        provenance={"kind": "internal_system"},
    )
    await asyncio.wait_for(
        runtime._event(runtime.send_entered, prefix_related_parent).wait(),
        timeout=1,
    )
    assert await manager.active_group_ids(PARENT) == []

    async with manager.quiesce_sessions([PARENT]):
        assert runtime.send_cancelled == []
        assert await manager.active_group_ids(prefix_related_parent) == [
            f"subagent:{prefix_related_parent}:prefix-related-task"
        ]

    runtime._event(runtime.send_release, prefix_related_parent).set()
    await manager.drain(timeout=1)
    assert runtime.sent == [prefix_related_parent]


@pytest.mark.asyncio
async def test_quiesce_sessions_waits_for_inflight_wake_registration() -> None:
    capture_entered = asyncio.Event()
    release_capture = asyncio.Event()

    class _BlockingSessionManager(_SessionManager):
        async def get_session(self, session_key: str) -> Any | None:
            capture_entered.set()
            await release_capture.wait()
            return None

    runtime = _BlockingSendRuntime()
    manager = BackgroundCompletionManager(session_manager=_BlockingSessionManager())
    sending = asyncio.create_task(
        manager.send_parent_wake(
            parent_session_key=PARENT,
            parent_task_id=PARENT_TASK,
            payloads=[],
            task_runtime=runtime,
            message="wake",
            provenance={"kind": "internal_system"},
        )
    )
    await asyncio.wait_for(capture_entered.wait(), timeout=1)

    fence_entered = asyncio.Event()
    release_fence = asyncio.Event()

    async def _hold_fence() -> None:
        async with manager.quiesce_sessions([PARENT]):
            fence_entered.set()
            await release_fence.wait()

    fence_task = asyncio.create_task(_hold_fence())
    await asyncio.sleep(0)
    assert not fence_entered.is_set()

    release_capture.set()
    await sending
    await asyncio.wait_for(fence_entered.wait(), timeout=1)
    release_fence.set()
    await fence_task

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[],
        task_runtime=runtime,
        message="same group must stay cancelled",
        provenance={"kind": "internal_system"},
    )
    await asyncio.sleep(0)
    assert PARENT not in runtime.send_entered


@pytest.mark.asyncio
async def test_concurrent_quiesce_sessions_receive_independent_state_changes() -> None:
    parents = (PARENT, OTHER_PARENT)
    capture_entered = {parent: asyncio.Event() for parent in parents}
    release_capture = {parent: asyncio.Event() for parent in parents}

    class _PerParentBlockingSessionManager(_SessionManager):
        async def get_session(self, session_key: str) -> Any | None:
            capture_entered[session_key].set()
            await release_capture[session_key].wait()
            return None

    runtime = _BlockingSendRuntime()
    manager = BackgroundCompletionManager(session_manager=_PerParentBlockingSessionManager())
    sending = [
        asyncio.create_task(
            manager.send_parent_wake(
                parent_session_key=parent,
                parent_task_id=f"task-{index}",
                payloads=[],
                task_runtime=runtime,
                message="wake",
                provenance={"kind": "internal_system"},
            )
        )
        for index, parent in enumerate(parents)
    ]
    await asyncio.gather(*(capture_entered[parent].wait() for parent in parents))

    fence_entered = {parent: asyncio.Event() for parent in parents}
    release_fence = {parent: asyncio.Event() for parent in parents}

    async def _hold_fence(parent: str) -> None:
        async with manager.quiesce_sessions([parent]):
            fence_entered[parent].set()
            await release_fence[parent].wait()

    fences = {parent: asyncio.create_task(_hold_fence(parent)) for parent in parents}
    await asyncio.sleep(0)
    assert not any(event.is_set() for event in fence_entered.values())

    release_capture[PARENT].set()
    await sending[0]
    await asyncio.wait_for(fence_entered[PARENT].wait(), timeout=1)
    assert not fence_entered[OTHER_PARENT].is_set()
    release_fence[PARENT].set()
    await fences[PARENT]

    release_capture[OTHER_PARENT].set()
    await sending[1]
    await asyncio.wait_for(fence_entered[OTHER_PARENT].wait(), timeout=1)
    release_fence[OTHER_PARENT].set()
    await fences[OTHER_PARENT]

    assert runtime.send_calls == []


@pytest.mark.asyncio
async def test_quiesce_sessions_keeps_nested_parent_fence_until_outer_exit() -> None:
    runtime = _BlockingSendRuntime()
    manager = BackgroundCompletionManager(session_manager=_SessionManager())

    async with manager.quiesce_sessions([PARENT]):
        async with manager.quiesce_sessions([PARENT]):
            await manager.send_parent_wake(
                parent_session_key=PARENT,
                parent_task_id="blocked-inner",
                payloads=[],
                task_runtime=runtime,
                message="blocked",
                provenance={"kind": "internal_system"},
            )
        await manager.send_parent_wake(
            parent_session_key=PARENT,
            parent_task_id="blocked-outer",
            payloads=[],
            task_runtime=runtime,
            message="still blocked",
            provenance={"kind": "internal_system"},
        )

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id="allowed-after-outer",
        payloads=[],
        task_runtime=runtime,
        message="allowed",
        provenance={"kind": "internal_system"},
    )
    await asyncio.wait_for(runtime._event(runtime.send_entered, PARENT).wait(), timeout=1)

    runtime._event(runtime.send_release, PARENT).set()
    await manager.drain(timeout=1)
    assert runtime.send_calls == [PARENT]
    assert runtime.sent == [PARENT]


@pytest.mark.asyncio
async def test_old_watcher_done_callback_does_not_untrack_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _ReplacementRuntime()
    manager = BackgroundCompletionManager(session_manager=_SessionManager())
    first_evicted = asyncio.Event()
    release_first_watcher = asyncio.Event()
    real_evict = manager._evict_group
    evict_count = 0

    async def _pause_first_evict(group_id: str) -> None:
        nonlocal evict_count
        await real_evict(group_id)
        evict_count += 1
        if evict_count == 1:
            first_evicted.set()
            await release_first_watcher.wait()

    monkeypatch.setattr(manager, "_evict_group", _pause_first_evict)

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[],
        task_runtime=runtime,
        message="first",
        provenance={"kind": "internal_system"},
    )
    await _wait_until(lambda: runtime.send_count == 1)
    runtime.synthesis_release["synthesis-1"].set()
    await asyncio.wait_for(first_evicted.wait(), timeout=1)

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[],
        task_runtime=runtime,
        message="replacement",
        provenance={"kind": "internal_system"},
    )
    await _wait_until(lambda: runtime.send_count == 2)
    release_first_watcher.set()
    await _wait_until(lambda: len(manager._watch_tasks) == 1)

    async with manager.quiesce_sessions([PARENT]):
        pass

    assert runtime.wait_cancelled == ["synthesis-2"]


@pytest.mark.asyncio
async def test_shielded_quiesce_finishes_target_watcher_cancel_cleanup() -> None:
    runtime = _BlockingSendRuntime()
    cleanup_release = asyncio.Event()
    runtime.cancel_cleanup_release[PARENT] = cleanup_release
    manager = BackgroundCompletionManager(session_manager=_SessionManager())

    await manager.send_parent_wake(
        parent_session_key=PARENT,
        parent_task_id=PARENT_TASK,
        payloads=[],
        task_runtime=runtime,
        message="wake",
        provenance={"kind": "internal_system"},
    )
    await asyncio.wait_for(runtime._event(runtime.send_entered, PARENT).wait(), timeout=1)

    fence_entered = asyncio.Event()
    release_fence = asyncio.Event()

    async def _operation() -> None:
        async with manager.quiesce_sessions([PARENT]):
            fence_entered.set()
            await release_fence.wait()

    operation = asyncio.create_task(_operation())

    async def _caller() -> None:
        await asyncio.shield(operation)

    for _ in range(2):
        caller = asyncio.create_task(_caller())
        await asyncio.sleep(0)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

    assert not operation.done()
    assert not fence_entered.is_set()

    cleanup_release.set()
    await asyncio.wait_for(fence_entered.wait(), timeout=1)
    release_fence.set()
    await operation
    assert runtime.send_cancelled == [PARENT]


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
