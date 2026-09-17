"""Per-session pending queue overflow policy tests.

Covers configurable ``pending_overflow_policy`` with two
behaviours and per-channel override hook surfaced for noisy realtime
adapters.

- ``reject_newest`` (default): preserves legacy ``TaskQueueFullError`` raise.
- ``drop_oldest``: evicts the oldest QUEUED pending task on the same
  session and accepts the new turn. The evicted task is marked
  ``CANCELLED`` with ``terminal_reason="dropped_by_overflow"``.

Tests use ``max_concurrency=1`` plus a gating event so the running
task stays running while the queue fills, isolating overflow behaviour
from execution scheduling.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import (
    PendingOverflowPolicy,
    TaskQueueFullError,
    TaskRuntime,
)
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus


def _make_envelope(session_key: str = "agent-1::sess-overflow") -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id="agent-1",
        session_key=session_key,
        input_provenance={"kind": "test"},
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

    async def list_tasks(**_: Any) -> list[AgentTaskRecord]:
        return list(task_db.values())

    storage.create_agent_task = create
    storage.update_agent_task = update
    storage.get_agent_task = get
    storage.list_agent_tasks = list_tasks
    return storage


def _make_runtime(
    *,
    turn_handler: Callable[..., Awaitable[Any]],
    max_pending_per_session: int = 2,
    pending_overflow_policy: PendingOverflowPolicy | str = (
        PendingOverflowPolicy.REJECT_NEWEST
    ),
) -> TaskRuntime:
    return TaskRuntime(
        storage=_make_storage(),
        turn_handler=turn_handler,
        max_concurrency=1,
        max_pending_per_session=max_pending_per_session,
        pending_overflow_policy=pending_overflow_policy,
    )


@pytest.mark.asyncio
async def test_reject_newest_raises_on_overflow() -> None:
    """Default policy preserves legacy TaskQueueFullError raise."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(_run: Any) -> None:
        started.set()
        await release.wait()

    rt = _make_runtime(
        turn_handler=slow_handler,
        max_pending_per_session=2,
        pending_overflow_policy=PendingOverflowPolicy.REJECT_NEWEST,
    )

    env = _make_envelope("agent-1::sess-reject")
    first = await rt.enqueue(env, "first")
    await asyncio.wait_for(started.wait(), timeout=1.0)

    # Fill the pending queue: cap is 2 so two more enqueues occupy the queue.
    second = await rt.enqueue(env, "second")
    third = await rt.enqueue(env, "third")

    # Fourth enqueue exceeds the cap.
    with pytest.raises(TaskQueueFullError):
        await rt.enqueue(env, "fourth")

    release.set()
    await rt.wait(first.task_id, timeout=2.0)
    await rt.wait(second.task_id, timeout=2.0)
    await rt.wait(third.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_drop_oldest_evicts_oldest_pending_and_accepts_new() -> None:
    """drop_oldest evicts the oldest QUEUED pending task and admits the new turn."""
    started = asyncio.Event()
    release = asyncio.Event()
    seen_messages: list[str] = []

    async def slow_handler(run: Any) -> None:
        if not started.is_set():
            started.set()
            await release.wait()
        seen_messages.append(run.message)

    rt = _make_runtime(
        turn_handler=slow_handler,
        max_pending_per_session=2,
        pending_overflow_policy=PendingOverflowPolicy.DROP_OLDEST,
    )

    env = _make_envelope("agent-1::sess-drop")
    first = await rt.enqueue(env, "first")  # becomes RUNNING immediately
    await asyncio.wait_for(started.wait(), timeout=1.0)

    second = await rt.enqueue(env, "second")  # PENDING
    third = await rt.enqueue(env, "third")  # PENDING — fills cap

    # Fourth enqueue triggers overflow under drop_oldest. Expect:
    # - "second" evicted (oldest pending)
    # - "fourth" admitted
    fourth = await rt.enqueue(env, "fourth")

    # Drain the runtime so terminal records settle.
    release.set()
    second_record = await rt.wait(second.task_id, timeout=2.0)
    third_record = await rt.wait(third.task_id, timeout=2.0)
    fourth_record = await rt.wait(fourth.task_id, timeout=2.0)
    first_record = await rt.wait(first.task_id, timeout=2.0)

    assert second_record.status == AgentTaskStatus.CANCELLED
    assert second_record.terminal_reason == "dropped_by_overflow"
    assert third_record.status == AgentTaskStatus.SUCCEEDED
    assert fourth_record.status == AgentTaskStatus.SUCCEEDED
    assert first_record.status == AgentTaskStatus.SUCCEEDED
    # "second" is the only one that never reaches the handler.
    assert "second" not in seen_messages
    assert seen_messages == ["first", "third", "fourth"]


@pytest.mark.asyncio
async def test_drop_oldest_closes_evicted_input_disposition() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    context_updates: list[tuple[str, str, dict[str, Any]]] = []
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def slow_handler(run: Any) -> None:
        if run.message == "first":
            started.set()
            await release.wait()

    async def update_turn_context(
        session_key: str,
        message_id: str,
        context: dict[str, Any],
    ) -> bool:
        context_updates.append((session_key, message_id, dict(context)))
        return True

    async def emit(session_key: str, name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, name, payload))

    storage = _make_storage()
    storage.update_transcript_turn_context = update_turn_context
    rt = TaskRuntime(
        storage=storage,
        turn_handler=slow_handler,
        event_emitter=emit,
        max_concurrency=1,
        max_pending_per_session=1,
        pending_overflow_policy=PendingOverflowPolicy.DROP_OLDEST,
    )
    env = _make_envelope("agent-1::sess-drop-identity")
    first = await rt.enqueue(env, "first")
    await asyncio.wait_for(started.wait(), timeout=1.0)
    victim_env = replace(
        env,
        metadata={
            "client_request_id": "request-victim",
            "client_message_id": "client-victim",
            "surface_id": "tui:test",
        },
    )
    victim = await rt.enqueue(
        victim_env,
        "victim",
        task_id="turn-overflow-victim",
        persisted_user_message_id="message-overflow-victim",
    )

    replacement = await rt.enqueue(env, "replacement")
    victim_record = await rt.wait(victim.task_id, timeout=2.0)
    assert victim_record.status == AgentTaskStatus.CANCELLED
    assert context_updates == [
        (
            env.session_key,
            "message-overflow-victim",
            {
                "turn_id": victim.task_id,
                "client_request_id": "request-victim",
                "client_message_id": "client-victim",
                "surface_id": "tui:test",
                "intent": "send",
                "disposition": "cancelled",
                "revision": 2,
            },
        )
    ]
    disposition = next(
        payload for _session, name, payload in events if name == "session.event.input_disposition"
    )
    assert disposition["turn_id"] == victim.task_id
    assert disposition["client_request_id"] == "request-victim"
    assert disposition["disposition"] == "cancelled"
    assert disposition["terminal_reason"] == "dropped_by_overflow"

    release.set()
    await rt.wait(first.task_id, timeout=2.0)
    await rt.wait(replacement.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_drop_oldest_does_not_evict_running_task() -> None:
    """Running tasks are protected — only QUEUED pending entries are eligible."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(_run: Any) -> None:
        if not started.is_set():
            started.set()
            await release.wait()

    rt = _make_runtime(
        turn_handler=slow_handler,
        max_pending_per_session=1,
        pending_overflow_policy=PendingOverflowPolicy.DROP_OLDEST,
    )

    env = _make_envelope("agent-1::sess-running")
    first = await rt.enqueue(env, "first")  # RUNNING
    await asyncio.wait_for(started.wait(), timeout=1.0)

    second = await rt.enqueue(env, "second")  # PENDING (fills cap=1)

    # Third enqueue must drop "second" (the only QUEUED pending), not the
    # RUNNING "first".
    third = await rt.enqueue(env, "third")

    release.set()
    first_record = await rt.wait(first.task_id, timeout=2.0)
    second_record = await rt.wait(second.task_id, timeout=2.0)
    third_record = await rt.wait(third.task_id, timeout=2.0)

    assert first_record.status == AgentTaskStatus.SUCCEEDED
    assert second_record.status == AgentTaskStatus.CANCELLED
    assert second_record.terminal_reason == "dropped_by_overflow"
    assert third_record.status == AgentTaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_per_call_overflow_policy_overrides_default() -> None:
    """The overflow_policy kwarg on enqueue overrides the runtime default."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(_run: Any) -> None:
        if not started.is_set():
            started.set()
            await release.wait()

    rt = _make_runtime(
        turn_handler=slow_handler,
        max_pending_per_session=1,
        pending_overflow_policy=PendingOverflowPolicy.REJECT_NEWEST,
    )

    env = _make_envelope("agent-1::sess-override")
    first = await rt.enqueue(env, "first")
    await asyncio.wait_for(started.wait(), timeout=1.0)
    second = await rt.enqueue(env, "second")

    # Default policy would raise; per-call override switches to drop_oldest.
    third = await rt.enqueue(
        env,
        "third",
        overflow_policy=PendingOverflowPolicy.DROP_OLDEST,
    )

    release.set()
    second_record = await rt.wait(second.task_id, timeout=2.0)
    third_record = await rt.wait(third.task_id, timeout=2.0)
    first_record = await rt.wait(first.task_id, timeout=2.0)

    assert second_record.status == AgentTaskStatus.CANCELLED
    assert second_record.terminal_reason == "dropped_by_overflow"
    assert third_record.status == AgentTaskStatus.SUCCEEDED
    assert first_record.status == AgentTaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_apply_overflow_policy_public_entry_point() -> None:
    """apply_overflow_policy enforces the cap without enqueueing a new turn."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(_run: Any) -> None:
        if not started.is_set():
            started.set()
            await release.wait()

    rt = _make_runtime(
        turn_handler=slow_handler,
        max_pending_per_session=1,
        pending_overflow_policy=PendingOverflowPolicy.REJECT_NEWEST,
    )

    env = _make_envelope("agent-1::sess-apply")
    first = await rt.enqueue(env, "first")
    await asyncio.wait_for(started.wait(), timeout=1.0)
    second = await rt.enqueue(env, "second")

    # Public entry point with drop_oldest evicts "second"; the queue is now
    # empty so a follow-up enqueue under reject_newest succeeds.
    await rt.apply_overflow_policy(
        env.session_key, policy=PendingOverflowPolicy.DROP_OLDEST
    )
    third = await rt.enqueue(env, "third")

    release.set()
    second_record = await rt.wait(second.task_id, timeout=2.0)
    third_record = await rt.wait(third.task_id, timeout=2.0)
    first_record = await rt.wait(first.task_id, timeout=2.0)

    assert second_record.status == AgentTaskStatus.CANCELLED
    assert second_record.terminal_reason == "dropped_by_overflow"
    assert third_record.status == AgentTaskStatus.SUCCEEDED
    assert first_record.status == AgentTaskStatus.SUCCEEDED


def test_invalid_policy_string_rejected() -> None:
    """Bad policy strings are rejected at construction time."""

    async def noop(_run: Any) -> None:
        return None

    with pytest.raises(ValueError, match="pending_overflow_policy"):
        TaskRuntime(
            storage=_make_storage(),
            turn_handler=noop,
            pending_overflow_policy="not-a-policy",
        )


@pytest.mark.asyncio
async def test_invalid_per_call_policy_rejected() -> None:
    async def noop(_run: Any) -> None:
        return None

    rt = _make_runtime(
        turn_handler=noop,
        max_pending_per_session=2,
        pending_overflow_policy=PendingOverflowPolicy.REJECT_NEWEST,
    )

    env = _make_envelope("agent-1::sess-invalid")
    with pytest.raises(ValueError, match="overflow_policy"):
        await rt.enqueue(env, "x", overflow_policy="bogus")
