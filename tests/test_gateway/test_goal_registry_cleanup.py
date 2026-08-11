"""Stress contracts for reclaimable Goal orchestration registries."""

from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.gateway.config import GoalConfig
from opensquilla.gateway.goal_service import GoalService
from opensquilla.gateway.task_runtime import TaskRuntime

_SESSION_COUNT = 100
_OPERATIONS_PER_SESSION = 10


class _NoGoalStorage:
    """Hold the first idle read per session so later requests must coalesce."""

    def __init__(self, expected_sessions: int) -> None:
        self.expected_sessions = expected_sessions
        self.calls: Counter[str] = Counter()
        self.started: set[str] = set()
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_goal(self, session_key: str) -> None:
        self.calls[session_key] += 1
        if session_key not in self.started:
            self.started.add(session_key)
            if len(self.started) == self.expected_sessions:
                self.all_started.set()
            await self.release.wait()
        return None


class _SubscribedConnections:
    def __init__(self, conn_id: str) -> None:
        self._conn_id = conn_id

    def get_message_subscribers(self, _session_key: str) -> set[str]:
        return {self._conn_id}


async def _noop_turn_handler(_run: Any) -> None:
    return


def _runtime_ephemeral_registries(runtime: TaskRuntime) -> dict[str, Any]:
    """Snapshot registries that must drain after automatic admission work."""

    return {
        "tasks": dict(runtime._tasks),
        "drivers": {
            key: set(tasks) for key, tasks in runtime._driver_tasks_by_session.items()
        },
        "reservations": {
            key: list(items) for key, items in runtime._reservations_by_session.items()
        },
        "pending": {
            key: list(items) for key, items in runtime._pending_by_session.items()
        },
        "running": dict(runtime._running_by_session),
        "overflow_victims": set(runtime._reserved_overflow_victims),
        "last_envelopes": dict(runtime._last_envelope_by_session),
        "last_envelope_task_ids": dict(runtime._last_envelope_task_id_by_session),
    }


@pytest.mark.asyncio
async def test_goal_registries_reclaim_after_one_thousand_idle_and_fence_operations(
) -> None:
    """Goal-only coordination state returns to baseline across many sessions.

    This deliberately admits no AgentTask: settled turn cleanup is covered by
    TaskRuntime lifecycle tests.  The scope here is the Goal-added idle,
    admission, user-intent, transition-lock, and execution-lease registries.
    """

    session_keys = [
        f"agent:main:webchat:goal-registry-{index}" for index in range(_SESSION_COUNT)
    ]
    storage = _NoGoalStorage(expected_sessions=len(session_keys))
    runtime = TaskRuntime(storage=storage, turn_handler=_noop_turn_handler)
    conn_id = "goal-registry-owner"
    service = GoalService(
        storage=storage,
        session_manager=SimpleNamespace(),
        task_runtime=runtime,
        event_emitter=None,
        subscription_manager=_SubscribedConnections(conn_id),
        config=GoalConfig(),
    )

    baseline_runtime = _runtime_ephemeral_registries(runtime)
    transition_active: Counter[str] = Counter()
    transition_peak: Counter[str] = Counter()
    intents_ready = 0
    all_intents_ready = asyncio.Event()

    async def exercise_fences(session_key: str) -> None:
        nonlocal intents_ready
        async with runtime.explicit_ingress_intent(session_key):
            intents_ready += 1
            if intents_ready == _SESSION_COUNT * _OPERATIONS_PER_SESSION:
                all_intents_ready.set()
            await all_intents_ready.wait()
            async with runtime.collect_admission(session_key):
                async with runtime.automatic_ingress_fence(session_key) as allowed:
                    assert allowed is False
                    async with service._lock(session_key):
                        transition_active[session_key] += 1
                        transition_peak[session_key] = max(
                            transition_peak[session_key],
                            transition_active[session_key],
                        )
                        await asyncio.sleep(0)
                        transition_active[session_key] -= 1

    workers = [
        asyncio.create_task(exercise_fences(session_key))
        for session_key in session_keys
        for _ in range(_OPERATIONS_PER_SESSION)
    ]
    await asyncio.gather(*workers)

    assert transition_peak == Counter(dict.fromkeys(session_keys, 1))
    assert runtime._ingress_intent_states == {}
    assert runtime._collect_admission_locks == {}
    assert service._transition_locks == {}

    # Block the first read for every session, then add nine requests per key.
    # This creates 1,000 idle-evaluation requests while retaining only one
    # worker plus one dirty follow-up evaluation for each session.
    for session_key in session_keys:
        service.schedule_idle_evaluation(session_key)
    await asyncio.wait_for(storage.all_started.wait(), timeout=5.0)
    first_kicks = tuple(service._kick_tasks.values())
    assert len(first_kicks) == _SESSION_COUNT

    for session_key in session_keys:
        for _ in range(_OPERATIONS_PER_SESSION - 1):
            service.schedule_idle_evaluation(session_key)

    assert service._kick_dirty == set(session_keys)
    assert tuple(service._kick_tasks.values()) == first_kicks
    storage.release.set()
    await asyncio.gather(*first_kicks)

    assert storage.calls == Counter(dict.fromkeys(session_keys, 2))
    assert service._kick_tasks == {}
    assert service._kick_dirty == set()

    principal = SimpleNamespace(
        token_public_id="registry-test-owner",
        guest_owner_id=None,
        is_owner=True,
        role="operator",
    )
    ctx = SimpleNamespace(conn_id=conn_id, principal=principal, agent_id="main")
    for index, session_key in enumerate(session_keys):
        service._install_lease(
            ctx,
            goal=SimpleNamespace(
                session_key=session_key,
                session_id=f"session-{index}",
                session_epoch=1,
                goal_id=f"goal-{index}",
            ),
            source_kind="web",
        )
    assert len(service._leases) == _SESSION_COUNT

    await asyncio.gather(
        *(service.on_subscription_lost(conn_id, session_key) for session_key in session_keys)
    )

    assert service._leases == {}
    assert service._continuity_grants == {}
    assert service._transition_locks == {}
    assert runtime._ingress_intent_states == {}
    assert runtime._collect_admission_locks == {}
    assert _runtime_ephemeral_registries(runtime) == baseline_runtime

    # These two dictionaries are intentionally stable per-session lock caches,
    # not ephemeral Goal registries.  Terminal eviction would risk splitting
    # callers across old and new lock objects, so this gate deliberately does
    # not require `_session_locks` or `_session_execution_locks` to shrink.
    await service.close()
    assert service._continuity_grants == {}
    await runtime.shutdown(cancel=True, timeout=1.0)
