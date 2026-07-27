"""Tests for the TaskRuntime terminal-state dict leak fix.

Verifies that, at terminal state, the four short-lived tracking dicts
(``_tasks``, ``_running_by_session``, ``_pending_by_session``,
``_last_envelope_by_session``) drop the task / session_key, while
``_session_locks`` is intentionally retained to prevent split-brain on
rapid re-enqueue. Also covers exception-path cleanup and a 10 000-task
tracemalloc-bounded soak.
"""

from __future__ import annotations

import asyncio
import gc
import tracemalloc
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock

import pytest

from opensquilla.gateway import task_runtime
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import AgentTaskRecord
from opensquilla.session.turn_context import current_turn_context

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_envelope(session_key: str = "agent-1::sess-1") -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="test",
        agent_id="agent-1",
        session_key=session_key,
        input_provenance={"kind": "test"},
    )


def _make_storage() -> Any:
    """Minimal storage mock."""
    storage = MagicMock()
    task_db: dict[str, AgentTaskRecord] = {}

    async def create(record: AgentTaskRecord) -> None:
        task_db[record.task_id] = record

    async def update(task_id: str, **kwargs: Any) -> None:
        rec = task_db.get(task_id)
        if rec is None:
            return
        for k, v in kwargs.items():
            if hasattr(rec, k):
                object.__setattr__(rec, k, v)

    async def get(task_id: str) -> AgentTaskRecord | None:
        return task_db.get(task_id)

    async def list_tasks(**_: Any) -> list[AgentTaskRecord]:
        return list(task_db.values())

    turn_context_updates: list[tuple[str, str, dict[str, Any]]] = []

    async def update_turn_context(
        session_key: str,
        message_id: str,
        context: dict[str, Any],
    ) -> bool:
        turn_context_updates.append((session_key, message_id, dict(context)))
        return True

    storage.create_agent_task = create
    storage.update_agent_task = update
    storage.get_agent_task = get
    storage.list_agent_tasks = list_tasks
    storage.update_transcript_turn_context = update_turn_context
    storage.turn_context_updates = turn_context_updates
    return storage


def _make_runtime(
    turn_handler: Callable[..., Awaitable[Any]] | None = None,
    max_concurrency: int = 4,
    max_pending_per_session: int | None = 64,
) -> TaskRuntime:
    async def _default_handler(_run: Any) -> None:
        pass

    return TaskRuntime(
        storage=_make_storage(),
        turn_handler=turn_handler or _default_handler,
        max_concurrency=max_concurrency,
        max_pending_per_session=max_pending_per_session,
    )


# ---------------------------------------------------------------------------
# terminal_clears_all_dicts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_terminal_clears_all_dicts() -> None:
    """After a task succeeds, tracking dicts (except _session_locks) must not contain its key.

    ``_session_locks`` is intentionally NOT cleaned at terminal to prevent
    split-brain under concurrent enqueue. All other dicts are cleaned.
    """
    rt = _make_runtime()
    env = _make_envelope("agent-1::sess-a")
    handle = await rt.enqueue(env, "hello")
    await rt.wait(handle.task_id, timeout=2.0)

    sk = env.session_key
    assert handle.task_id not in rt._tasks
    assert sk not in rt._running_by_session
    assert sk not in rt._pending_by_session
    # _session_locks is intentionally retained: never pop while _execute may
    # still hold the lock; prevents split-brain on rapid re-enqueue.
    assert sk not in rt._last_envelope_by_session


@pytest.mark.asyncio
async def test_preallocated_turn_identity_is_propagated_to_handler() -> None:
    observed: list[dict[str, Any] | None] = []

    async def _handler(_run: Any) -> None:
        observed.append(current_turn_context())

    rt = _make_runtime(turn_handler=_handler)
    env = _make_envelope("agent-1::identity")
    env = replace(
        env,
        metadata={
            "client_message_id": "client-1",
            "surface_id": "tui:test",
        },
    )
    handle = await rt.enqueue(env, "hello", task_id="turn-preallocated")
    await rt.wait(handle.task_id, timeout=2.0)

    assert handle.task_id == "turn-preallocated"
    assert observed == [
        {
            "turn_id": "turn-preallocated",
            "client_message_id": "client-1",
            "surface_id": "tui:test",
            "intent": "send",
            "disposition": "applied",
            "revision": 1,
        }
    ]


@pytest.mark.asyncio
async def test_identity_aware_turn_emits_applied_input_disposition() -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _emit(session_key: str, name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, name, payload))

    async def _handler(_run: Any) -> None:
        return None

    rt = TaskRuntime(
        storage=_make_storage(),
        turn_handler=_handler,
        event_emitter=_emit,
    )
    env = replace(
        _make_envelope("agent-1::identity-event"),
        metadata={
            "client_message_id": "client-1",
            "surface_id": "tui:test",
        },
    )
    handle = await rt.enqueue(
        env,
        "hello",
        task_id="turn-preallocated",
        persisted_user_message_id="message-1",
    )
    await rt.wait(handle.task_id, timeout=2.0)

    disposition_events = [
        event for event in events if event[1] == "session.event.input_disposition"
    ]
    assert disposition_events == [
        (
            env.session_key,
            "session.event.input_disposition",
            {
                "session_key": env.session_key,
                "user_message_id": "message-1",
                "turn_id": "turn-preallocated",
                "client_message_id": "client-1",
                "surface_id": "tui:test",
                "intent": "send",
                "disposition": "applied",
                "revision": 1,
            },
        )
    ]


@pytest.mark.asyncio
async def test_identity_aware_collect_rebinds_each_prompt_to_the_running_turn() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    runs: list[tuple[str, str, list[dict[str, Any]], str | None]] = []
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _handler(run: Any) -> None:
        runs.append((run.task_id, run.message, run.attachments, run.semantic_message))
        if run.message == "blocker":
            started.set()
            await release.wait()

    async def _emit(session_key: str, name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, name, payload))

    storage = _make_storage()
    rt = TaskRuntime(
        storage=storage,
        turn_handler=_handler,
        event_emitter=_emit,
        max_concurrency=1,
    )
    env = _make_envelope("agent-1::identity-collect")
    blocker = await rt.enqueue(env, "blocker")
    await asyncio.wait_for(started.wait(), timeout=2.0)

    first_env = replace(
        env,
        metadata={"client_message_id": "client-1", "surface_id": "tui:test"},
    )
    second_env = replace(
        env,
        metadata={"client_message_id": "client-2", "surface_id": "tui:test"},
    )
    first = await rt.enqueue(
        first_env,
        "first collected input",
        attachments=[{"name": "first.txt"}],
        mode="collect",
        semantic_message="first semantic",
        task_id="turn-collect-1",
        persisted_user_message_id="message-1",
    )
    second = await rt.enqueue(
        second_env,
        "second collected input",
        attachments=[{"name": "second.txt"}],
        mode="collect",
        semantic_message="second semantic",
        task_id="turn-collect-2",
        persisted_user_message_id="message-2",
    )

    assert first.task_id == "turn-collect-1"
    assert second.task_id == first.task_id
    assert storage.turn_context_updates[-1] == (
        env.session_key,
        "message-2",
        {
            "turn_id": first.task_id,
            "client_message_id": "client-2",
            "surface_id": "tui:test",
            "intent": "send",
            "disposition": "queued",
            "target_turn_id": first.task_id,
            "revision": 2,
        },
    )

    release.set()
    await rt.wait(blocker.task_id, timeout=2.0)
    await rt.wait(first.task_id, timeout=2.0)
    assert runs == [
        (blocker.task_id, "blocker", [], None),
        (
            first.task_id,
            "first collected input\nsecond collected input",
            [{"name": "first.txt"}, {"name": "second.txt"}],
            "first semantic\n\nsecond semantic",
        ),
    ]
    applied = [
        context
        for _session, message_id, context in storage.turn_context_updates
        if message_id == "message-2" and context.get("disposition") == "applied"
    ]
    assert applied == [
        {
            "turn_id": first.task_id,
            "client_message_id": "client-2",
            "surface_id": "tui:test",
            "intent": "send",
            "disposition": "applied",
            "target_turn_id": first.task_id,
            "revision": 2,
        }
    ]
    assert any(
        name == "session.event.input_disposition"
        and payload.get("user_message_id") == "message-2"
        and payload.get("disposition") == "applied"
        for _session, name, payload in events
    )


@pytest.mark.asyncio
async def test_prestart_cancel_closes_every_collected_prompt_identity() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def _handler(run: Any) -> None:
        if run.message == "blocker":
            started.set()
            await release.wait()

    storage = _make_storage()
    rt = TaskRuntime(storage=storage, turn_handler=_handler, max_concurrency=1)
    env = _make_envelope("agent-1::identity-collect-cancel")
    blocker = await rt.enqueue(env, "blocker")
    await asyncio.wait_for(started.wait(), timeout=2.0)

    first = await rt.enqueue(
        replace(env, metadata={"client_message_id": "client-1", "surface_id": "tui:test"}),
        "first",
        mode="collect",
        task_id="turn-collect-cancel-1",
        persisted_user_message_id="message-1",
    )
    second = await rt.enqueue(
        replace(env, metadata={"client_message_id": "client-2", "surface_id": "tui:test"}),
        "second",
        mode="collect",
        task_id="turn-collect-cancel-2",
        persisted_user_message_id="message-2",
    )
    assert second.task_id == first.task_id

    assert await rt.cancel(task_id=first.task_id) == 1
    record = await rt.wait(first.task_id, timeout=2.0)
    assert record.status.value == "cancelled"
    terminal = {
        message_id: context
        for _session, message_id, context in storage.turn_context_updates
        if context.get("disposition") == "cancelled"
    }
    assert set(terminal) == {"message-1", "message-2"}
    assert terminal["message-1"]["turn_id"] == first.task_id
    assert terminal["message-2"] == {
        "turn_id": first.task_id,
        "client_message_id": "client-2",
        "surface_id": "tui:test",
        "intent": "send",
        "disposition": "cancelled",
        "target_turn_id": first.task_id,
        "revision": 2,
    }

    release.set()
    await rt.wait(blocker.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_collect_details_failure_does_not_reject_an_accepted_input() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    runs: list[str] = []

    async def _handler(run: Any) -> None:
        runs.append(run.message)
        if run.message == "blocker":
            started.set()
            await release.wait()

    storage = _make_storage()
    update_agent_task = storage.update_agent_task

    async def _fail_collected_details(task_id: str, **kwargs: Any) -> None:
        if (kwargs.get("details") or {}).get("collected"):
            raise RuntimeError("diagnostic write unavailable")
        await update_agent_task(task_id, **kwargs)

    storage.update_agent_task = _fail_collected_details
    rt = TaskRuntime(storage=storage, turn_handler=_handler, max_concurrency=1)
    env = _make_envelope("agent-1::collect-details-failure")
    blocker = await rt.enqueue(env, "blocker")
    await asyncio.wait_for(started.wait(), timeout=2.0)
    first = await rt.enqueue(env, "one", mode="collect")

    second = await rt.enqueue(env, "two", mode="collect")
    assert second.task_id == first.task_id

    release.set()
    await rt.wait(blocker.task_id, timeout=2.0)
    await rt.wait(first.task_id, timeout=2.0)
    assert runs == ["blocker", "one\ntwo"]


@pytest.mark.asyncio
async def test_identity_free_collect_preserves_legacy_coalescing() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    runs: list[str] = []

    async def _handler(run: Any) -> None:
        runs.append(run.message)
        if run.message == "blocker":
            started.set()
            await release.wait()

    rt = _make_runtime(turn_handler=_handler, max_concurrency=1)
    env = _make_envelope("agent-1::legacy-collect")
    blocker = await rt.enqueue(env, "blocker")
    await asyncio.wait_for(started.wait(), timeout=2.0)

    first = await rt.enqueue(env, "one", mode="collect")
    second = await rt.enqueue(env, "two", mode="collect")
    assert second.task_id == first.task_id

    release.set()
    await rt.wait(blocker.task_id, timeout=2.0)
    await rt.wait(first.task_id, timeout=2.0)
    assert runs == ["blocker", "one\ntwo"]


@pytest.mark.asyncio
async def test_prestart_cancel_closes_primary_input_disposition() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _handler(run: Any) -> None:
        if run.message == "blocker":
            started.set()
            await release.wait()

    async def _emit(session_key: str, name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, name, payload))

    storage = _make_storage()
    rt = TaskRuntime(
        storage=storage,
        turn_handler=_handler,
        event_emitter=_emit,
        max_concurrency=1,
    )
    env = _make_envelope("agent-1::prestart-cancel")
    blocker = await rt.enqueue(env, "blocker")
    await asyncio.wait_for(started.wait(), timeout=2.0)
    queued_env = replace(
        env,
        metadata={"client_message_id": "client-cancel", "surface_id": "tui:test"},
    )
    queued = await rt.enqueue(
        queued_env,
        "queued",
        task_id="turn-cancelled-before-start",
        persisted_user_message_id="message-cancelled-before-start",
    )

    assert await rt.cancel(task_id=queued.task_id) == 1
    record = await rt.wait(queued.task_id, timeout=2.0)
    assert record.status.value == "cancelled"
    assert storage.turn_context_updates[-1] == (
        env.session_key,
        "message-cancelled-before-start",
        {
            "turn_id": queued.task_id,
            "client_message_id": "client-cancel",
            "surface_id": "tui:test",
            "intent": "send",
            "disposition": "cancelled",
            "revision": 2,
        },
    )
    disposition = next(
        payload
        for _session, name, payload in events
        if name == "session.event.input_disposition" and payload.get("turn_id") == queued.task_id
    )
    assert disposition["disposition"] == "cancelled"
    assert disposition["terminal_reason"] == "cancelled_before_start"

    release.set()
    await rt.wait(blocker.task_id, timeout=2.0)


@pytest.mark.asyncio
async def test_shutdown_closes_queued_primary_input_disposition() -> None:
    started = asyncio.Event()

    async def _handler(run: Any) -> None:
        if run.message == "blocker":
            started.set()
            await asyncio.Event().wait()

    storage = _make_storage()
    rt = TaskRuntime(storage=storage, turn_handler=_handler, max_concurrency=1)
    env = _make_envelope("agent-1::shutdown-queued")
    await rt.enqueue(env, "blocker")
    await asyncio.wait_for(started.wait(), timeout=2.0)
    queued_env = replace(
        env,
        metadata={"client_message_id": "client-shutdown", "surface_id": "tui:test"},
    )
    queued = await rt.enqueue(
        queued_env,
        "queued",
        task_id="turn-shutdown-before-start",
        persisted_user_message_id="message-shutdown-before-start",
    )

    await rt.shutdown(cancel=True, timeout=2.0)
    record = await rt.status(queued.task_id)
    assert record.status.value == "cancelled"
    assert storage.turn_context_updates[-1][1:] == (
        "message-shutdown-before-start",
        {
            "turn_id": queued.task_id,
            "client_message_id": "client-shutdown",
            "surface_id": "tui:test",
            "intent": "send",
            "disposition": "cancelled",
            "revision": 2,
        },
    )


@pytest.mark.asyncio
async def test_shutdown_timeout_rejects_unstarted_primary_input() -> None:
    started = asyncio.Event()

    async def _handler(run: Any) -> None:
        if run.message == "blocker":
            started.set()
            await asyncio.Event().wait()

    storage = _make_storage()
    rt = TaskRuntime(storage=storage, turn_handler=_handler, max_concurrency=1)
    env = _make_envelope("agent-1::shutdown-abandoned")
    await rt.enqueue(env, "blocker")
    await asyncio.wait_for(started.wait(), timeout=2.0)
    queued_env = replace(
        env,
        metadata={"client_message_id": "client-abandoned", "surface_id": "tui:test"},
    )
    queued = await rt.enqueue(
        queued_env,
        "queued",
        task_id="turn-abandoned-before-start",
        persisted_user_message_id="message-abandoned-before-start",
    )

    await rt.shutdown(cancel=False, timeout=0.01)
    record = await rt.status(queued.task_id)
    assert record.status.value == "abandoned"
    assert storage.turn_context_updates[-1][1:] == (
        "message-abandoned-before-start",
        {
            "turn_id": queued.task_id,
            "client_message_id": "client-abandoned",
            "surface_id": "tui:test",
            "intent": "send",
            "disposition": "rejected",
            "revision": 2,
        },
    )


@pytest.mark.asyncio
async def test_steer_is_drained_by_running_turn_provider() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    drained: list[str] = []

    async def _handler(run: Any) -> None:
        started.set()
        await release.wait()
        drained.extend(run.pending_input_provider.drain_pending())

    rt = _make_runtime(turn_handler=_handler)
    env = _make_envelope("agent-1::steer-drain")
    handle = await rt.enqueue(env, "first")
    await started.wait()

    assert await rt.active_task_id(env.session_key) == handle.task_id
    accepted = await rt.steer(
        env.session_key,
        "change direction",
        persisted_user_message_id="msg-steer",
    )
    assert accepted == handle.task_id

    release.set()
    await rt.wait(handle.task_id, timeout=2.0)
    assert drained == ["change direction"]
    applied = [
        context
        for _session, message_id, context in rt._storage.turn_context_updates
        if message_id == "msg-steer" and context.get("disposition") == "applied"
    ]
    assert applied == [
        {
            "turn_id": handle.task_id,
            "client_message_id": None,
            "surface_id": None,
            "intent": "steer",
            "disposition": "applied",
            "target_turn_id": handle.task_id,
            "revision": 2,
        }
    ]


@pytest.mark.asyncio
async def test_undrained_late_steer_is_promoted_to_followup() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    followup_seen = asyncio.Event()
    runs: list[str] = []

    async def _handler(run: Any) -> None:
        runs.append(run.message)
        if run.message == "first":
            first_started.set()
            await release_first.wait()
            return
        followup_seen.set()

    rt = _make_runtime(turn_handler=_handler)
    env = _make_envelope("agent-1::steer-fallback")
    handle = await rt.enqueue(env, "first")
    await first_started.wait()
    assert await rt.steer(
        env.session_key,
        "too late for a tool boundary",
        persisted_user_message_id="msg-late",
    ) == handle.task_id

    release_first.set()
    await rt.wait(handle.task_id, timeout=2.0)
    await asyncio.wait_for(followup_seen.wait(), timeout=2.0)
    assert runs == ["first", "too late for a tool boundary"]
    promoted = [
        context
        for _session, message_id, context in rt._storage.turn_context_updates
        if message_id == "msg-late" and context.get("disposition") == "promoted"
    ]
    assert len(promoted) == 1
    assert promoted[0]["turn_id"] != handle.task_id
    assert promoted[0]["promoted_from_turn_id"] == handle.task_id


@pytest.mark.asyncio
async def test_undrained_steer_survives_failed_active_turn_as_followup() -> None:
    first_started = asyncio.Event()
    fail_first = asyncio.Event()
    followup_seen = asyncio.Event()
    runs: list[str] = []

    async def _handler(run: Any) -> None:
        runs.append(run.message)
        if run.message == "first":
            first_started.set()
            await fail_first.wait()
            raise RuntimeError("provider failed after accepting steer")
        followup_seen.set()

    rt = _make_runtime(turn_handler=_handler)
    env = _make_envelope("agent-1::steer-error-fallback")
    handle = await rt.enqueue(env, "first")
    await first_started.wait()
    assert await rt.steer(
        env.session_key,
        "continue despite provider failure",
        persisted_user_message_id="msg-after-error",
    ) == handle.task_id

    fail_first.set()
    await rt.wait(handle.task_id, timeout=2.0)
    await asyncio.wait_for(followup_seen.wait(), timeout=2.0)
    assert runs == ["first", "continue despite provider failure"]


@pytest.mark.asyncio
async def test_failed_late_steer_promotion_is_durable_and_emits_recovery_state() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    release_queued = asyncio.Event()
    rejected_seen = asyncio.Event()
    events: list[tuple[str, str, dict[str, Any]]] = []

    async def _handler(run: Any) -> None:
        if run.message == "first":
            first_started.set()
            await release_first.wait()
            return
        await release_queued.wait()

    async def _emit(session_key: str, name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, name, payload))
        if (
            name == "session.event.input_disposition"
            and payload.get("failure_code") == "STEER_PROMOTION_QUEUE_FULL"
        ):
            rejected_seen.set()

    storage = _make_storage()
    rt = TaskRuntime(
        storage=storage,
        turn_handler=_handler,
        event_emitter=_emit,
        max_concurrency=1,
        max_pending_per_session=1,
    )
    env = _make_envelope("agent-1::steer-promotion-full")
    first = await rt.enqueue(env, "first")
    await first_started.wait()
    queued = await rt.enqueue(env, "already queued")
    assert await rt.steer(
        env.session_key,
        "accepted but cannot promote",
        persisted_user_message_id="msg-rejected",
        client_message_id="client-rejected",
        surface_id="tui:test",
    ) == first.task_id

    release_first.set()
    await asyncio.wait_for(rejected_seen.wait(), timeout=2.0)

    rejected = [
        context
        for _session, message_id, context in storage.turn_context_updates
        if message_id == "msg-rejected" and context.get("disposition") == "rejected"
    ]
    assert rejected == [
        {
            "turn_id": first.task_id,
            "client_message_id": "client-rejected",
            "surface_id": "tui:test",
            "intent": "steer",
            "disposition": "rejected",
            "target_turn_id": first.task_id,
            "revision": 2,
            "promoted_from_turn_id": first.task_id,
        }
    ]
    failure_event = next(
        payload
        for _session, name, payload in events
        if name == "session.event.input_disposition"
        and payload.get("failure_code") == "STEER_PROMOTION_QUEUE_FULL"
    )
    assert failure_event["retryable"] is True
    assert failure_event["recovery"] == "resend_after_queue_drains"

    release_queued.set()
    await rt.wait(queued.task_id, timeout=2.0)


# ---------------------------------------------------------------------------
# cancel_clears_dicts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_clears_dicts() -> None:
    """After a task is cancelled, all five tracking dicts must not contain its key."""
    started = asyncio.Event()
    blocker = asyncio.Event()

    async def _blocking_handler(_run: Any) -> None:
        started.set()
        await blocker.wait()  # blocks until test cancels

    rt = _make_runtime(turn_handler=_blocking_handler)
    env = _make_envelope("agent-1::sess-b")
    handle = await rt.enqueue(env, "hello")

    # Wait for the handler to actually start, then cancel.
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await rt.cancel(task_id=handle.task_id)
    await rt.wait(handle.task_id, timeout=2.0)

    sk = env.session_key
    assert handle.task_id not in rt._tasks
    assert sk not in rt._running_by_session
    assert sk not in rt._pending_by_session
    # _session_locks is intentionally retained.
    assert sk not in rt._last_envelope_by_session


@pytest.mark.asyncio
async def test_cancel_closes_steer_window_before_disposition_persistence() -> None:
    """A steer cannot enter after cancellation reclaimed the accepted inputs."""

    started = asyncio.Event()
    blocker = asyncio.Event()
    cleanup_persisting = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def _blocking_handler(_run: Any) -> None:
        started.set()
        await blocker.wait()

    storage = _make_storage()
    update_turn_context = storage.update_transcript_turn_context

    async def _blocking_turn_context_update(
        session_key: str,
        message_id: str,
        context: dict[str, Any],
    ) -> bool:
        if message_id == "msg-before-cancel":
            cleanup_persisting.set()
            await release_cleanup.wait()
        return await update_turn_context(session_key, message_id, context)

    storage.update_transcript_turn_context = _blocking_turn_context_update
    rt = TaskRuntime(storage=storage, turn_handler=_blocking_handler)
    env = _make_envelope("agent-1::cancel-steer-race")
    handle = await rt.enqueue(env, "first")
    await asyncio.wait_for(started.wait(), timeout=2.0)

    assert await rt.steer(
        env.session_key,
        "accepted before cancellation",
        persisted_user_message_id="msg-before-cancel",
    ) == handle.task_id
    runtime_task = rt._tasks[handle.task_id]

    assert await rt.cancel(task_id=handle.task_id) == 1
    # cancel() is the acknowledgement boundary. Even before the cancelled
    # task gets another event-loop slice, steer must already reject input.
    assert await rt.steer(
        env.session_key,
        "racing immediately after cancel acknowledgement",
        persisted_user_message_id="msg-after-cancel-ack",
    ) is None
    await asyncio.wait_for(cleanup_persisting.wait(), timeout=2.0)

    # Cancellation has reclaimed the earlier input and is waiting on storage.
    # The acceptance window must already be closed, so this cannot become an
    # orphaned pending item after the task is marked terminal.
    assert await rt.steer(
        env.session_key,
        "racing during cancellation cleanup",
        persisted_user_message_id="msg-during-cancel",
    ) is None

    release_cleanup.set()
    await rt.wait(handle.task_id, timeout=2.0)

    cancelled = [
        context
        for _session, message_id, context in storage.turn_context_updates
        if message_id == "msg-before-cancel"
    ]
    assert cancelled == [
        {
            "turn_id": handle.task_id,
            "client_message_id": None,
            "surface_id": None,
            "intent": "steer",
            "disposition": "cancelled",
            "target_turn_id": handle.task_id,
            "revision": 2,
        }
    ]
    assert runtime_task.pending_input_provider.reclaim_all() == []


# ---------------------------------------------------------------------------
# session_lock_kept_during_pending
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_lock_kept_during_pending() -> None:
    """_session_locks must NOT be removed while another task is still pending."""
    first_started = asyncio.Event()
    first_release = asyncio.Event()

    async def _slow_handler(_run: Any) -> None:
        first_started.set()
        await first_release.wait()

    # Only 1 concurrency slot so the second task stays pending.
    rt = _make_runtime(turn_handler=_slow_handler, max_concurrency=1)
    env = _make_envelope("agent-1::sess-c")

    handle1 = await rt.enqueue(env, "first")
    await asyncio.wait_for(first_started.wait(), timeout=2.0)

    # Enqueue second task — it will be QUEUED (pending) while first is running.
    handle2 = await rt.enqueue(env, "second")

    sk = env.session_key
    # Session lock must exist because there is still a pending task.
    assert sk in rt._session_locks

    # Now let the first task finish.
    first_release.set()
    await rt.wait(handle1.task_id, timeout=2.0)

    # The lock should still exist because the second task is still alive.
    assert sk in rt._session_locks

    # Wait for second task to finish.
    await rt.wait(handle2.task_id, timeout=2.0)

    # _session_locks is intentionally retained after all tasks complete;
    # do not assert its absence here.


@pytest.mark.asyncio
async def test_older_terminal_task_keeps_newer_route_envelope_cached() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    async def handler(run: Any) -> None:
        if run.message == "first":
            first_started.set()
            await release_first.wait()
        elif run.message == "second":
            second_started.set()
            await release_second.wait()

    rt = _make_runtime(turn_handler=handler, max_concurrency=1)
    session_key = "agent-1::route-cache-race"
    first_envelope = _make_envelope(session_key)
    second_envelope = RouteEnvelope(
        source_kind=SourceKind.WEB,
        source_name="newer-route",
        agent_id="agent-1",
        session_key=session_key,
        input_provenance={"kind": "newer-test-route"},
    )

    first = await rt.enqueue(first_envelope, "first")
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    second = await rt.enqueue(second_envelope, "second")
    second_runtime_task = rt._tasks[second.task_id]
    assert rt._last_envelope_by_session[session_key] is second_runtime_task.envelope

    release_first.set()
    await rt.wait(first.task_id, timeout=1.0)
    await asyncio.wait_for(second_started.wait(), timeout=1.0)

    # The older task may clean up only the envelope it installed. The newer
    # task's route remains available to TaskRuntime.send until that task ends.
    assert rt._last_envelope_by_session[session_key] is second_runtime_task.envelope

    release_second.set()
    await rt.wait(second.task_id, timeout=1.0)
    assert session_key not in rt._last_envelope_by_session


# ---------------------------------------------------------------------------
# exception path cleans up
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exception_path_clears_dicts() -> None:
    """Even when the turn handler raises, cleanup must run for 4 tracking dicts.

    ``_session_locks`` is intentionally NOT cleared on terminal: retaining
    the lock prevents split-brain when a new enqueue races with _execute's
    post-terminal cleanup. All other 4 dicts (``_tasks``,
    ``_running_by_session``, ``_pending_by_session``,
    ``_last_envelope_by_session``) must be cleaned up.
    """

    async def _failing_handler(_run: Any) -> None:
        raise RuntimeError("deliberate failure")

    rt = _make_runtime(turn_handler=_failing_handler)
    env = _make_envelope("agent-1::sess-d")
    handle = await rt.enqueue(env, "hello")
    await rt.wait(handle.task_id, timeout=2.0)

    sk = env.session_key
    assert handle.task_id not in rt._tasks
    assert sk not in rt._running_by_session
    # _session_locks is intentionally retained after terminal: prevents
    # split-brain on rapid re-enqueue; lock is cheap and bounded per session_key.
    assert sk not in rt._pending_by_session
    assert sk not in rt._last_envelope_by_session


# ---------------------------------------------------------------------------
# no_leak_under_load (tracemalloc quantitative)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_leak_under_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """10 000 tasks, each <=50 ms; dict sizes after GC must be within ±2 of baseline."""
    num_tasks = 10_000
    session_count = 50  # rotate sessions to mimic real load
    monkeypatch.setattr(task_runtime, "_emit_metric", lambda *_args, **_kwargs: None)

    async def _instant_handler(_run: Any) -> None:
        pass  # returns immediately — well under 50 ms

    rt = _make_runtime(
        turn_handler=_instant_handler,
        max_concurrency=32,
        max_pending_per_session=None,
    )

    # --- baseline snapshot (before any tasks) ---
    gc.collect()
    tracemalloc.start()
    snap_before = tracemalloc.take_snapshot()

    baseline_tasks = len(rt._tasks)
    baseline_pending = len(rt._pending_by_session)
    baseline_running = len(rt._running_by_session)
    baseline_envelope = len(rt._last_envelope_by_session)

    # --- run 10 000 tasks ---
    handles = []
    for i in range(num_tasks):
        sk = f"agent-1::sess-load-{i % session_count}"
        env = _make_envelope(sk)
        h = await rt.enqueue(env, f"msg-{i}")
        handles.append(h)

    # Wait for all to complete under one shared deadline. Giving every waiter
    # its own timer schedules 10 000 timeout callbacks and makes this leak
    # check sensitive to event-loop scheduling on slower CI runners.
    async with asyncio.timeout(60.0):
        await asyncio.gather(*(rt.wait(h.task_id) for h in handles))

    # --- post-GC snapshot ---
    gc.collect()
    snap_after = tracemalloc.take_snapshot()
    tracemalloc.stop()

    after_tasks = len(rt._tasks)
    after_locks = len(rt._session_locks)
    after_pending = len(rt._pending_by_session)
    after_running = len(rt._running_by_session)
    after_envelope = len(rt._last_envelope_by_session)

    tolerance = 2
    assert abs(after_tasks - baseline_tasks) <= tolerance, (
        f"_tasks leaked: baseline={baseline_tasks}, after={after_tasks}"
    )
    # _session_locks is intentionally NOT cleaned at terminal to prevent
    # split-brain on rapid re-enqueue.  The dict grows by # unique session_keys
    # (capped at session_count=50 here), not by # tasks.  We verify it is bounded
    # by session_count rather than by num_tasks.
    assert after_locks <= session_count + tolerance, (
        f"_session_locks grew beyond unique session count: {after_locks} > {session_count}"
    )
    assert abs(after_pending - baseline_pending) <= tolerance, (
        f"_pending_by_session leaked: baseline={baseline_pending}, after={after_pending}"
    )
    assert abs(after_running - baseline_running) <= tolerance, (
        f"_running_by_session leaked: baseline={baseline_running}, after={after_running}"
    )
    assert abs(after_envelope - baseline_envelope) <= tolerance, (
        f"_last_envelope_by_session leaked: baseline={baseline_envelope}, after={after_envelope}"
    )

    # Confirm memory allocation delta is reasonable (no catastrophic growth).
    # Informational only — the dict-size assertions above are authoritative.
    # 10 000 asyncio tasks create significant transient allocation for
    # Task/Future/Event objects; allow up to 200 MB of incidental growth.
    top_stats = snap_after.compare_to(snap_before, "lineno")
    total_added = sum(s.size_diff for s in top_stats if s.size_diff > 0)
    assert total_added < 200 * 1024 * 1024, (
        f"Unexpected memory growth: {total_added / 1024:.1f} KB"
    )
