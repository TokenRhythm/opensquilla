from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from opensquilla.gateway.background_completion import BackgroundCompletionManager
from opensquilla.gateway.routing import RouteEnvelope, SourceKind, tool_context_from_envelope
from opensquilla.gateway.subagent_announce import (
    _build_terminal_group_payloads,
    _send_parent_wake,
    _tracker,
    announce_subagent_completion,
    set_background_completion_manager,
)
from opensquilla.gateway.task_runtime import SubagentCompletionEvent, TaskRuntime
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionIntent,
    SessionStatus,
)
from opensquilla.session.storage import SessionStorage, StaleEpochError
from opensquilla.tools.builtin import sessions as sessions_tool
from opensquilla.tools.types import current_tool_context

PARENT_KEY = "agent:main:webchat:completion-owner"
CHILD_KEY = "agent:worker:subagent:completion-owner"
PARENT_TASK_ID = "parent-task-owner"


@pytest_asyncio.fixture
async def process_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[SessionManager, SessionManager]]:
    monkeypatch.setenv(
        "OPENSQUILLA_SESSION_ARCHIVE_DIR",
        str(tmp_path / "archives"),
    )
    db_path = tmp_path / "subagent-completion-owner.db"
    process_a_storage = SessionStorage(str(db_path))
    process_b_storage = SessionStorage(str(db_path))
    await process_a_storage.connect()
    await process_b_storage.connect()
    process_a = SessionManager(process_a_storage, inject_time_prefix=False)
    process_b = SessionManager(process_b_storage, inject_time_prefix=False)
    try:
        yield process_a, process_b
    finally:
        set_background_completion_manager(None)
        _tracker.evict(PARENT_KEY)
        sessions_tool.set_session_manager(None)
        sessions_tool.set_task_runtime(None)
        await process_b_storage.close()
        await process_a_storage.close()


async def _create_lineage(
    manager: SessionManager,
) -> tuple[Any, Any]:
    parent = await manager.create(PARENT_KEY, agent_id="main")
    child = await manager.create(
        CHILD_KEY,
        agent_id="worker",
        spawned_by=PARENT_KEY,
        parent_session_key=PARENT_KEY,
        origin={
            "kind": "subagent",
            "parent_session_key": PARENT_KEY,
            "parent_task_id": PARENT_TASK_ID,
        },
    )
    return parent, child


def _completion_event(parent: Any, child: Any) -> SubagentCompletionEvent:
    return SubagentCompletionEvent(
        parent_session_key=PARENT_KEY,
        child_session_key=CHILD_KEY,
        task_id="child-task-owner",
        status=AgentTaskStatus.SUCCEEDED,
        terminal_reason="completed",
        agent_id="worker",
        parent_task_id=PARENT_TASK_ID,
        child_session_id=child.session_id,
        child_session_epoch=int(child.epoch or 0),
        parent_session_id=parent.session_id,
        parent_session_epoch=int(parent.epoch or 0),
    )


@pytest.mark.asyncio
async def test_spawn_rejects_parent_replaced_before_admission(
    process_pair: tuple[SessionManager, SessionManager],
) -> None:
    process_a, process_b = process_pair
    admitted = await process_a.create(PARENT_KEY, agent_id="main")
    context = tool_context_from_envelope(
        RouteEnvelope(
            source_kind=SourceKind.WEB,
            source_name="test",
            agent_id="main",
            session_key=PARENT_KEY,
            session_id=admitted.session_id,
            metadata={"task_id": PARENT_TASK_ID},
            session_epoch=int(admitted.epoch or 0),
        ),
        is_owner=True,
    )
    replacement, rotated = await process_b.apply_intent(
        PARENT_KEY,
        SessionIntent.RESET_SAME_KEY,
    )
    assert rotated is True
    sessions_tool.set_session_manager(process_a)

    class _NeverEnqueue:
        async def enqueue(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("stale parent must not create or enqueue a child")

    sessions_tool.set_task_runtime(_NeverEnqueue())
    token = current_tool_context.set(context)
    try:
        with pytest.raises(StaleEpochError, match="session read"):
            await sessions_tool.sessions_spawn(agent_id="worker", task="stale spawn")
    finally:
        current_tool_context.reset(token)

    current = await process_b.get_session(PARENT_KEY)
    assert current is not None
    assert current.session_id == replacement.session_id
    assert await process_b.list_sessions(spawned_by=PARENT_KEY) == []


@pytest.mark.asyncio
async def test_sessions_yield_binds_terminal_group_wake_to_parent_owner(
    process_pair: tuple[SessionManager, SessionManager],
) -> None:
    process_a, _process_b = process_pair
    parent, child = await _create_lineage(process_a)
    await process_a.append_message(CHILD_KEY, "assistant", "child result")
    await process_a.finish(
        CHILD_KEY,
        expected_session_id=child.session_id,
        expected_session_epoch=int(child.epoch or 0),
    )
    admitted_envelopes: list[RouteEnvelope] = []

    class _CaptureRuntime:
        async def send_with_envelope(
            self,
            envelope: RouteEnvelope,
            _message: str,
            *,
            provenance: dict[str, Any],
        ) -> SimpleNamespace:
            assert provenance["source_tool"] == "subagent_completion"
            admitted_envelopes.append(envelope)
            return SimpleNamespace(task_id="parent-wake")

    sessions_tool.set_session_manager(process_a)
    sessions_tool.set_task_runtime(_CaptureRuntime())
    context = tool_context_from_envelope(
        RouteEnvelope(
            source_kind=SourceKind.WEB,
            source_name="test",
            agent_id="main",
            session_key=PARENT_KEY,
            session_id=parent.session_id,
            metadata={"task_id": PARENT_TASK_ID},
            session_epoch=int(parent.epoch or 0),
        ),
        is_owner=True,
    )
    token = current_tool_context.set(context)
    try:
        payload = json.loads(await sessions_tool.sessions_yield())
    finally:
        current_tool_context.reset(token)

    assert payload["status"] == "yielded"
    assert len(admitted_envelopes) == 1
    assert (
        admitted_envelopes[0].session_id,
        admitted_envelopes[0].session_epoch,
    ) == (parent.session_id, int(parent.epoch or 0))


@pytest.mark.asyncio
async def test_sessions_yield_does_not_aggregate_replacement_child_transcript(
    process_pair: tuple[SessionManager, SessionManager],
) -> None:
    process_a, process_b = process_pair
    parent = await process_a.create(PARENT_KEY, agent_id="main")
    child_task_id = "child-task-group-owner"
    child = await process_a.create(
        CHILD_KEY,
        agent_id="worker",
        spawned_by=PARENT_KEY,
        parent_session_key=PARENT_KEY,
        origin={
            "kind": "subagent",
            "parent_session_key": PARENT_KEY,
            "parent_task_id": PARENT_TASK_ID,
            "task_id": child_task_id,
        },
    )
    await process_a.storage.create_agent_task(
        AgentTaskRecord(
            task_id=child_task_id,
            session_key=CHILD_KEY,
            agent_id="worker",
            source_kind="subagent",
            run_kind="subagent",
            status=AgentTaskStatus.SUCCEEDED,
            terminal_reason="completed",
            details={
                "session_id": child.session_id,
                "session_epoch": int(child.epoch or 0),
                "metadata": {"parent_task_id": PARENT_TASK_ID},
            },
        ),
        expected_session_id=child.session_id,
        expected_session_epoch=int(child.epoch or 0),
    )
    await process_a.append_message(CHILD_KEY, "assistant", "retired child result")
    await process_a.finish(
        CHILD_KEY,
        expected_session_id=child.session_id,
        expected_session_epoch=int(child.epoch or 0),
    )
    replacement, rotated = await process_b.apply_intent(
        CHILD_KEY,
        SessionIntent.RESET_SAME_KEY,
        agent_id="worker",
    )
    assert rotated is True
    await process_b.append_message(CHILD_KEY, "assistant", "B SECRET")

    admitted: list[tuple[RouteEnvelope, str]] = []

    class _CaptureRuntime:
        async def send_with_envelope(
            self,
            envelope: RouteEnvelope,
            message: str,
            *,
            provenance: dict[str, Any],
        ) -> SimpleNamespace:
            del provenance
            admitted.append((envelope, message))
            return SimpleNamespace(task_id="parent-wake")

    sessions_tool.set_session_manager(process_a)
    sessions_tool.set_task_runtime(_CaptureRuntime())
    context = tool_context_from_envelope(
        RouteEnvelope(
            source_kind=SourceKind.WEB,
            source_name="test",
            agent_id="main",
            session_key=PARENT_KEY,
            session_id=parent.session_id,
            metadata={"task_id": PARENT_TASK_ID},
            session_epoch=int(parent.epoch or 0),
        ),
        is_owner=True,
    )
    token = current_tool_context.set(context)
    try:
        response = json.loads(await sessions_tool.sessions_yield())
    finally:
        current_tool_context.reset(token)

    current_child = await process_b.get_session(CHILD_KEY)
    assert current_child is not None
    assert current_child.session_id == replacement.session_id
    assert response["status"] == "yielded"
    assert admitted == []


@pytest.mark.asyncio
async def test_group_child_with_stable_task_id_cannot_downgrade_to_ownerless_read() -> None:
    reads: list[str] = []

    class _SummaryOnlyStorage:
        async def list_agent_tasks_for_sessions(
            self,
            _keys: list[str],
            limit_per_session: int = 10,
        ) -> dict[str, list[SimpleNamespace]]:
            del limit_per_session
            return {
                CHILD_KEY: [
                    SimpleNamespace(
                        task_id="child-task-modern",
                        session_key=CHILD_KEY,
                        run_kind="subagent",
                        status=AgentTaskStatus.SUCCEEDED,
                    )
                ]
            }

    class _SummaryOnlyManager:
        _storage = _SummaryOnlyStorage()

        async def list_sessions(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return [
                {
                    "session_key": CHILD_KEY,
                    "session_id": "replacement-owner-b",
                    "epoch": 2,
                    "status": "done",
                    "spawned_by": PARENT_KEY,
                    "origin": {
                        "kind": "subagent",
                        "parent_task_id": PARENT_TASK_ID,
                        "task_id": "child-task-modern",
                    },
                }
            ]

        async def read_transcript(self, session_key: str, limit: int = 50) -> list[Any]:
            del limit
            reads.append(session_key)
            return []

    payloads = await _build_terminal_group_payloads(
        parent_session_key=PARENT_KEY,
        parent_task_id=PARENT_TASK_ID,
        session_manager=_SummaryOnlyManager(),
    )

    assert payloads is None
    assert reads == []


@pytest.mark.asyncio
async def test_stale_completion_cannot_finish_replacement_child(
    process_pair: tuple[SessionManager, SessionManager],
) -> None:
    process_a, process_b = process_pair
    parent, child = await _create_lineage(process_a)
    replacement, rotated = await process_b.apply_intent(
        CHILD_KEY,
        SessionIntent.RESET_SAME_KEY,
        agent_id="worker",
    )
    assert rotated is True
    await process_b.append_message(CHILD_KEY, "assistant", "replacement result")
    emitted: list[dict[str, Any]] = []

    async def emit(_key: str, _name: str, payload: dict[str, Any]) -> None:
        emitted.append(payload)

    with pytest.raises(StaleEpochError, match="finish"):
        await announce_subagent_completion(
            _completion_event(parent, child),
            session_manager=process_a,
            event_emitter=emit,
        )

    current = await process_b.get_session(CHILD_KEY)
    assert current is not None
    assert current.session_id == replacement.session_id
    assert current.status == SessionStatus.RUNNING
    assert [row.content for row in await process_b.get_transcript(CHILD_KEY)] == [
        "replacement result"
    ]
    assert await process_b.get_transcript(PARENT_KEY) == []
    assert emitted == []


@pytest.mark.asyncio
async def test_stale_completion_cannot_read_replacement_child_result(
    process_pair: tuple[SessionManager, SessionManager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_a, process_b = process_pair
    parent, child = await _create_lineage(process_a)
    await process_a.append_message(CHILD_KEY, "assistant", "retired result")
    original_finish = process_a.finish
    replacement_owner: list[str] = []

    async def finish_then_reset(
        session_key: str,
        status: str = SessionStatus.DONE,
        *,
        expected_session_id: str | None = None,
        expected_session_epoch: int | None = None,
    ) -> Any:
        assert (expected_session_id, expected_session_epoch) == (
            child.session_id,
            int(child.epoch or 0),
        )
        finished = await original_finish(
            session_key,
            status,
            expected_session_id=expected_session_id,
            expected_session_epoch=expected_session_epoch,
        )
        replacement, rotated = await process_b.apply_intent(
            CHILD_KEY,
            SessionIntent.RESET_SAME_KEY,
            agent_id="worker",
        )
        assert rotated is True
        replacement_owner.append(replacement.session_id)
        await process_b.update(
            CHILD_KEY,
            status=SessionStatus.RUNNING,
            ended_at=None,
            runtime_ms=None,
            expected_session_id=replacement.session_id,
            expected_session_epoch=int(replacement.epoch or 0),
        )
        await process_b.append_message(CHILD_KEY, "assistant", "replacement secret")
        return finished

    monkeypatch.setattr(process_a, "finish", finish_then_reset)

    with pytest.raises(StaleEpochError, match="transcript read"):
        await announce_subagent_completion(
            _completion_event(parent, child),
            session_manager=process_a,
        )

    current = await process_b.get_session(CHILD_KEY)
    assert current is not None
    assert current.session_id == replacement_owner[0]
    assert current.status == SessionStatus.RUNNING
    assert [row.content for row in await process_b.get_transcript(CHILD_KEY)] == [
        "replacement secret"
    ]
    assert await process_b.get_transcript(PARENT_KEY) == []


@pytest.mark.asyncio
async def test_stale_completion_cannot_append_to_replacement_parent(
    process_pair: tuple[SessionManager, SessionManager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_a, process_b = process_pair
    parent, child = await _create_lineage(process_a)
    await process_a.append_message(CHILD_KEY, "assistant", "child result")
    original_append = process_a.append_message
    replacement_owner: list[str] = []

    async def append_after_parent_reset(
        session_key: str,
        role: str,
        content: str,
        *,
        provenance: dict[str, Any] | None = None,
        expected_session_id: str | None = None,
        expected_session_epoch: int | None = None,
        **kwargs: Any,
    ) -> Any:
        assert session_key == PARENT_KEY
        assert (expected_session_id, expected_session_epoch) == (
            parent.session_id,
            int(parent.epoch or 0),
        )
        replacement, rotated = await process_b.apply_intent(
            PARENT_KEY,
            SessionIntent.RESET_SAME_KEY,
        )
        assert rotated is True
        replacement_owner.append(replacement.session_id)
        await process_b.append_message(PARENT_KEY, "user", "replacement input")
        return await original_append(
            session_key,
            role,
            content,
            provenance=provenance,
            expected_session_id=expected_session_id,
            expected_session_epoch=expected_session_epoch,
            **kwargs,
        )

    monkeypatch.setattr(process_a, "append_message", append_after_parent_reset)
    emitted: list[dict[str, Any]] = []

    async def emit(_key: str, _name: str, payload: dict[str, Any]) -> None:
        emitted.append(payload)

    class _NeverChannel:
        def get(self, _name: str) -> None:
            raise AssertionError("stale completion must not reach a parent channel")

    class _NeverWake:
        async def send(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("stale completion must not enqueue a parent wake")

    with pytest.raises(StaleEpochError, match="owner mismatch"):
        await announce_subagent_completion(
            _completion_event(parent, child),
            session_manager=process_a,
            event_emitter=emit,
            channel_manager=_NeverChannel(),
            task_runtime=_NeverWake(),
        )

    current = await process_b.get_session(PARENT_KEY)
    assert current is not None
    assert current.session_id == replacement_owner[0]
    assert current.status == SessionStatus.RUNNING
    assert [row.content for row in await process_b.get_transcript(PARENT_KEY)] == [
        "replacement input"
    ]
    assert emitted == []


@pytest.mark.asyncio
async def test_direct_channel_delivery_rechecks_parent_owner_after_event_emit(
    process_pair: tuple[SessionManager, SessionManager],
) -> None:
    process_a, process_b = process_pair
    parent = await process_a.create(
        PARENT_KEY,
        agent_id="main",
        last_channel="test",
        last_to="parent-a",
    )
    child = await process_a.create(
        CHILD_KEY,
        agent_id="worker",
        spawned_by=PARENT_KEY,
        parent_session_key=PARENT_KEY,
        origin={
            "kind": "subagent",
            "parent_session_key": PARENT_KEY,
            "parent_task_id": PARENT_TASK_ID,
        },
    )
    await process_a.append_message(CHILD_KEY, "assistant", "child result")
    delivered: list[Any] = []

    class _Adapter:
        async def send(self, message: Any) -> None:
            delivered.append(message)

    class _ChannelManager:
        def get(self, _name: str) -> _Adapter:
            return _Adapter()

    async def reset_parent_on_emit(
        _key: str,
        _name: str,
        _payload: dict[str, Any],
    ) -> None:
        replacement, rotated = await process_b.apply_intent(
            PARENT_KEY,
            SessionIntent.RESET_SAME_KEY,
        )
        assert rotated is True
        await process_b.update(
            PARENT_KEY,
            last_channel="test",
            last_to="parent-b",
            expected_session_id=replacement.session_id,
            expected_session_epoch=int(replacement.epoch or 0),
        )

    with pytest.raises(StaleEpochError, match="session read"):
        await announce_subagent_completion(
            _completion_event(parent, child),
            session_manager=process_a,
            event_emitter=reset_parent_on_emit,
            channel_manager=_ChannelManager(),
        )

    assert delivered == []


@pytest.mark.parametrize("use_background_manager", [False, True])
@pytest.mark.asyncio
async def test_parent_reset_after_completion_append_cannot_enqueue_replacement_wake(
    process_pair: tuple[SessionManager, SessionManager],
    use_background_manager: bool,
) -> None:
    process_a, process_b = process_pair
    parent, child = await _create_lineage(process_a)
    await process_a.append_message(CHILD_KEY, "assistant", "child result")
    handler_calls: list[str] = []

    async def handler(run: Any) -> None:
        handler_calls.append(run.session_key)

    runtime = TaskRuntime(storage=process_a.storage, turn_handler=handler)
    group_events: list[dict[str, Any]] = []

    async def emit_group(_key: str, _name: str, payload: dict[str, Any]) -> None:
        group_events.append(payload)

    background = (
        BackgroundCompletionManager(
            session_manager=process_a,
            event_emitter=emit_group,
        )
        if use_background_manager
        else None
    )
    set_background_completion_manager(background)
    _tracker.mark_closed(PARENT_KEY, PARENT_TASK_ID)
    replacement_owner: list[str] = []
    transport_payloads: list[dict[str, Any]] = []

    async def reset_parent_on_transport(
        _key: str,
        _name: str,
        payload: dict[str, Any],
    ) -> None:
        transport_payloads.append(payload)
        replacement, rotated = await process_b.apply_intent(
            PARENT_KEY,
            SessionIntent.RESET_SAME_KEY,
        )
        assert rotated is True
        replacement_owner.append(replacement.session_id)
        await process_b.append_message(PARENT_KEY, "user", "replacement input")

    try:
        if use_background_manager:
            await announce_subagent_completion(
                _completion_event(parent, child),
                session_manager=process_a,
                event_emitter=reset_parent_on_transport,
                task_runtime=runtime,
            )
            assert background is not None
            await background.drain(timeout=5)
        else:
            with pytest.raises(StaleEpochError, match="durable admission"):
                await announce_subagent_completion(
                    _completion_event(parent, child),
                    session_manager=process_a,
                    event_emitter=reset_parent_on_transport,
                    task_runtime=runtime,
                )

        current = await process_b.get_session(PARENT_KEY)
        assert current is not None
        assert current.session_id == replacement_owner[0]
        assert current.status == SessionStatus.RUNNING
        assert [row.content for row in await process_b.get_transcript(PARENT_KEY)] == [
            "replacement input"
        ]
        assert handler_calls == []
        assert transport_payloads[0]["session_id"] == parent.session_id
        assert transport_payloads[0]["epoch"] == int(parent.epoch or 0)
        if use_background_manager:
            assert group_events
            assert {
                (payload.get("session_id"), payload.get("epoch"))
                for payload in group_events
            } == {(parent.session_id, int(parent.epoch or 0))}
        else:
            assert group_events == []
        assert await process_b.storage.list_agent_tasks(
            session_key=PARENT_KEY,
            limit=10,
        ) == []
    finally:
        if background is not None:
            await background.close(timeout=5)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_id_only_completion_remains_compatible_and_owner_fields_stay_internal(
    process_pair: tuple[SessionManager, SessionManager],
) -> None:
    process_a, _process_b = process_pair
    parent, child = await _create_lineage(process_a)
    await process_a.append_message(CHILD_KEY, "assistant", "legacy result")
    event = _completion_event(parent, child)
    event = SubagentCompletionEvent(
        parent_session_key=event.parent_session_key,
        child_session_key=event.child_session_key,
        task_id=event.task_id,
        status=event.status,
        terminal_reason=event.terminal_reason,
        agent_id=event.agent_id,
        parent_task_id=event.parent_task_id,
        child_session_id=event.child_session_id,
        parent_session_id=event.parent_session_id,
    )

    await announce_subagent_completion(event, session_manager=process_a)

    current_child = await process_a.get_session(CHILD_KEY)
    assert current_child is not None
    assert current_child.status == SessionStatus.DONE
    parent_rows = await process_a.get_transcript(PARENT_KEY)
    assert len(parent_rows) == 1
    payload = json.loads(parent_rows[0].content or "{}")
    assert payload["result"]["text"] == "legacy result"
    assert "child_session_id" not in payload
    assert "child_session_epoch" not in payload
    assert "parent_session_id" not in payload
    assert "parent_session_epoch" not in payload


@pytest.mark.asyncio
async def test_exact_completion_rejects_kwargs_only_owner_proxy() -> None:
    calls: list[str] = []

    class _KwargsOnlyProxy:
        async def finish(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            calls.append("finish")

    event = SubagentCompletionEvent(
        parent_session_key=PARENT_KEY,
        child_session_key=CHILD_KEY,
        task_id="child-task-owner",
        status=AgentTaskStatus.SUCCEEDED,
        terminal_reason="completed",
        child_session_id="child-owner-a",
        child_session_epoch=2,
        parent_session_id="parent-owner-a",
        parent_session_epoch=4,
    )

    with pytest.raises(RuntimeError, match="exact session-owner operation"):
        await announce_subagent_completion(
            event,
            session_manager=_KwargsOnlyProxy(),
        )

    assert calls == []


@pytest.mark.asyncio
async def test_background_group_transport_keeps_parent_owner_through_success() -> None:
    parent_envelope = RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id="main",
        session_key=PARENT_KEY,
        session_id="parent-owner-a",
        session_epoch=7,
    )
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(_key: str, name: str, payload: dict[str, Any]) -> None:
        emitted.append((name, payload))

    class _SessionManager:
        async def get_session(
            self,
            _key: str,
            *,
            expected_session_id: str | None = None,
            expected_session_epoch: int | None = None,
        ) -> SimpleNamespace:
            assert (expected_session_id, expected_session_epoch) == (
                "parent-owner-a",
                7,
            )
            return SimpleNamespace(session_id="parent-owner-a", epoch=7)

    class _Runtime:
        async def wait(self, _task_id: str) -> SimpleNamespace:
            return SimpleNamespace(status=AgentTaskStatus.SUCCEEDED)

        async def send_with_envelope(
            self,
            envelope: RouteEnvelope,
            _message: str,
            **_kwargs: Any,
        ) -> SimpleNamespace:
            assert (envelope.session_id, envelope.session_epoch) == (
                "parent-owner-a",
                7,
            )
            return SimpleNamespace(task_id="synthesis-task")

    runtime = _Runtime()
    manager = BackgroundCompletionManager(
        session_manager=_SessionManager(),
        event_emitter=emit,
    )
    try:
        await manager.emit_waiting(
            parent_session_key=PARENT_KEY,
            parent_task_id=PARENT_TASK_ID,
            pending_count=0,
            parent_envelope=parent_envelope,
        )
        await manager.send_parent_wake(
            parent_session_key=PARENT_KEY,
            parent_task_id=PARENT_TASK_ID,
            payloads=[{"status": "succeeded"}],
            task_runtime=runtime,
            message="synthesize",
            provenance={"kind": "internal_system"},
            parent_envelope=parent_envelope,
        )
        await manager.drain(timeout=5)
    finally:
        await manager.close(timeout=5)

    assert [name.rsplit(".", 1)[-1] for name, _payload in emitted] == [
        "waiting",
        "synthesizing",
        "done",
    ]
    assert {
        (payload.get("session_id"), payload.get("epoch"))
        for _name, payload in emitted
    } == {("parent-owner-a", 7)}


@pytest.mark.asyncio
async def test_background_exact_owner_rejects_only_send_runtime() -> None:
    parent_envelope = RouteEnvelope(
        source_kind=SourceKind.SYSTEM,
        source_name="test",
        agent_id="main",
        session_key=PARENT_KEY,
        session_id="parent-owner-a",
        session_epoch=7,
    )
    sends: list[str] = []
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(_key: str, name: str, payload: dict[str, Any]) -> None:
        emitted.append((name, payload))

    class _SessionManager:
        async def get_session(self, _key: str) -> None:
            return None

    class _OnlySendRuntime:
        async def wait(self, _task_id: str) -> SimpleNamespace:
            return SimpleNamespace(status=AgentTaskStatus.SUCCEEDED)

        async def send(self, session_key: str, *_args: Any, **_kwargs: Any) -> None:
            sends.append(session_key)

    manager = BackgroundCompletionManager(
        session_manager=_SessionManager(),
        event_emitter=emit,
    )
    try:
        await manager.send_parent_wake(
            parent_session_key=PARENT_KEY,
            parent_task_id=PARENT_TASK_ID,
            payloads=[{"status": "succeeded"}],
            task_runtime=_OnlySendRuntime(),
            message="synthesize",
            provenance={"kind": "internal_system"},
            parent_envelope=parent_envelope,
        )
        await manager.drain(timeout=5)
    finally:
        await manager.close(timeout=5)

    assert sends == []
    assert [name.rsplit(".", 1)[-1] for name, _payload in emitted] == ["failed"]
    assert (emitted[0][1]["session_id"], emitted[0][1]["epoch"]) == (
        "parent-owner-a",
        7,
    )


@pytest.mark.asyncio
async def test_exact_parent_wake_rejects_kwargs_only_envelope_proxy() -> None:
    calls: list[str] = []

    class _DroppingRuntime:
        async def send_with_envelope(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            calls.append("send_with_envelope")

        async def send(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            calls.append("send")

    with pytest.raises(RuntimeError, match="owner-bound wake admission"):
        await _send_parent_wake(
            PARENT_KEY,
            None,
            [{"status": "succeeded"}],
            task_runtime=_DroppingRuntime(),
            parent_session_id="parent-owner-a",
            parent_session_epoch=7,
        )

    assert calls == []


@pytest.mark.asyncio
async def test_background_exact_parent_wake_rejects_kwargs_only_envelope_proxy() -> None:
    parent_envelope = RouteEnvelope(
        source_kind=SourceKind.SYSTEM,
        source_name="test",
        agent_id="main",
        session_key=PARENT_KEY,
        session_id="parent-owner-a",
        session_epoch=7,
    )
    calls: list[str] = []
    emitted: list[tuple[str, dict[str, Any]]] = []

    class _DroppingRuntime:
        async def wait(self, _task_id: str) -> SimpleNamespace:
            return SimpleNamespace(status=AgentTaskStatus.SUCCEEDED)

        async def send_with_envelope(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            calls.append("send_with_envelope")

        async def send(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            calls.append("send")

    async def emit(_key: str, name: str, payload: dict[str, Any]) -> None:
        emitted.append((name, payload))

    manager = BackgroundCompletionManager(
        session_manager=SimpleNamespace(),
        event_emitter=emit,
    )
    try:
        await manager.send_parent_wake(
            parent_session_key=PARENT_KEY,
            parent_task_id=PARENT_TASK_ID,
            payloads=[{"status": "succeeded"}],
            task_runtime=_DroppingRuntime(),
            message="synthesize",
            provenance={"kind": "internal_system"},
            parent_envelope=parent_envelope,
        )
        await manager.drain(timeout=5)
    finally:
        await manager.close(timeout=5)

    assert calls == []
    assert [name.rsplit(".", 1)[-1] for name, _payload in emitted] == ["failed"]
    assert (emitted[0][1]["session_id"], emitted[0][1]["epoch"]) == (
        "parent-owner-a",
        7,
    )


@pytest.mark.asyncio
async def test_background_final_delivery_rechecks_parent_owner(
    process_pair: tuple[SessionManager, SessionManager],
) -> None:
    process_a, process_b = process_pair
    parent = await process_a.create(
        PARENT_KEY,
        agent_id="main",
        last_channel="test",
        last_to="parent-a",
    )
    parent_envelope = RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id="main",
        session_key=PARENT_KEY,
        session_id=parent.session_id,
        session_epoch=int(parent.epoch or 0),
    )
    admitted = asyncio.Event()
    synthesis_release = asyncio.Event()
    delivered: list[Any] = []
    emitted: list[tuple[str, dict[str, Any]]] = []

    class _Adapter:
        async def send(self, message: Any) -> None:
            delivered.append(message)

    class _ChannelManager:
        def get(self, _name: str) -> _Adapter:
            return _Adapter()

    class _ControlledRuntime:
        async def wait(self, task_id: str) -> SimpleNamespace:
            if task_id == PARENT_TASK_ID:
                return SimpleNamespace(status=AgentTaskStatus.SUCCEEDED)
            assert task_id == "synthesis-task"
            await synthesis_release.wait()
            return SimpleNamespace(status=AgentTaskStatus.SUCCEEDED)

        async def send_with_envelope(
            self,
            envelope: RouteEnvelope,
            _message: str,
            *,
            provenance: dict[str, Any],
            stream_event_sink: Any,
            accepted_run_mode_override: Any | None,
        ) -> SimpleNamespace:
            del provenance, accepted_run_mode_override
            assert (envelope.session_id, envelope.session_epoch) == (
                parent.session_id,
                int(parent.epoch or 0),
            )
            await stream_event_sink(
                {
                    "type": "done",
                    "text": "STALE A FINAL",
                    "text_snapshot": "STALE A FINAL",
                }
            )
            admitted.set()
            return SimpleNamespace(task_id="synthesis-task")

    async def emit(_key: str, name: str, payload: dict[str, Any]) -> None:
        emitted.append((name, payload))

    manager = BackgroundCompletionManager(
        session_manager=process_a,
        event_emitter=emit,
        channel_manager_ref=lambda: _ChannelManager(),
    )
    try:
        await manager.send_parent_wake(
            parent_session_key=PARENT_KEY,
            parent_task_id=PARENT_TASK_ID,
            payloads=[{"status": "succeeded"}],
            task_runtime=_ControlledRuntime(),
            message="synthesize",
            provenance={"kind": "internal_system"},
            parent_envelope=parent_envelope,
        )
        await asyncio.wait_for(admitted.wait(), timeout=5)
        replacement, rotated = await process_b.apply_intent(
            PARENT_KEY,
            SessionIntent.RESET_SAME_KEY,
        )
        assert rotated is True
        assert replacement.session_id != parent.session_id
        synthesis_release.set()
        await manager.drain(timeout=5)
    finally:
        await manager.close(timeout=5)

    assert delivered == []
    assert [name.rsplit(".", 1)[-1] for name, _payload in emitted] == [
        "synthesizing",
        "failed",
    ]
    assert {
        (payload.get("session_id"), payload.get("epoch"))
        for _name, payload in emitted
    } == {(parent.session_id, int(parent.epoch or 0))}
