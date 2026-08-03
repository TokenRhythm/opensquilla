"""Goal continuation driver contracts (maybe_continue_goal + watcher registry).

WO-4b: real-assembly tests for the post-turn driver hook. Each test builds an
in-memory storage + real TaskRuntime + fake turn handler (same shape as
``test_goal_rpc.py``), starts a goal via ``goals.set``, then lets the driver
loop auto-enqueue follow-up ``goal_turn`` tasks and asserts the ledger
transitions (continue / complete / blocked / max_turns / watcher gate / idle
nudge).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig, GoalConfig
from opensquilla.gateway.goal_driver import (
    GOAL_CONTINUATION_MESSAGE,
    GoalWatcherRegistry,
    get_goal_watcher_registry,
    maybe_continue_goal,
)
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_goals import _handle_goals_set
from opensquilla.gateway.task_runtime import TaskRun, TaskRuntime
from opensquilla.session.goals import IDLE_PROGRESS_PROMPT
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskStatus
from opensquilla.session.storage import SessionStorage

SOURCE_KEY = "agent:main:webchat:goal-driver-source"
_WATCHER_CLIENT = "goal-driver-watcher"

_PRINCIPAL = Principal(
    role="operator",
    scopes=frozenset({"operator.admin"}),
    is_owner=True,
    authenticated=True,
)

_TurnHandler = Callable[[TaskRun], Awaitable[None]]


@dataclass
class _GoalDriverStack:
    storage: SessionStorage
    manager: SessionManager
    runtime: TaskRuntime
    context: RpcContext


@asynccontextmanager
async def _open_goal_driver_stack(
    db_path: Path,
    *,
    handler: _TurnHandler,
    goal_config: GoalConfig | None = None,
) -> AsyncIterator[_GoalDriverStack]:
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage, inject_time_prefix=False)
    runtime = TaskRuntime(
        storage=storage,
        turn_handler=handler,
        max_concurrency=1,
        running_heartbeat_interval_s=None,
        goal_config=goal_config,
    )
    context = RpcContext(
        conn_id="goal-driver-test",
        principal=_PRINCIPAL,
        config=GatewayConfig(
            workspace_dir=str(db_path.parent / "workspace"),
            memory={"flush_enabled": False},
            naming={"enabled": False},
        ),
        session_manager=manager,
        task_runtime=runtime,
    )
    await manager.create(SOURCE_KEY, agent_id="main")
    try:
        yield _GoalDriverStack(
            storage=storage,
            manager=manager,
            runtime=runtime,
            context=context,
        )
    finally:
        await runtime.shutdown(cancel=True, timeout=2.0)
        await storage.close()


async def _ignore_subscriber_event(*_args: Any, **_kwargs: Any) -> None:
    return None


class _MarkerScript:
    """Fake turn handler emitting one scripted assistant reply per turn.

    Each invocation appends the next marker text to the session transcript;
    the runtime's ``turn_context`` scope stamps the entry with the current
    task id so the driver's marker resolution (``_last_assistant_text``) can
    attribute it to exactly this turn. ``block_turn_index`` (1-based) lets a
    test park one turn in-flight to observe live plan-run claims.
    """

    def __init__(self, markers: list[str | None]) -> None:
        self.markers = list(markers)
        self.captured: list[TaskRun] = []
        self.manager: SessionManager | None = None
        self.block_turn_index: int | None = None
        self.block_started: asyncio.Event | None = None
        self.block_release: asyncio.Event | None = None

    async def __call__(self, run: TaskRun) -> None:
        self.captured.append(run)
        if (
            self.block_turn_index is not None
            and len(self.captured) == self.block_turn_index
        ):
            if self.block_started is not None:
                self.block_started.set()
            if self.block_release is not None:
                await self.block_release.wait()
        marker = self.markers.pop(0) if self.markers else None
        if marker is not None and self.manager is not None:
            await self.manager.append_message(SOURCE_KEY, "assistant", marker)


async def _wait_until(
    predicate: Callable[[], Awaitable[bool]],
    timeout: float = 5.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def _wait_for_goal_status(
    storage: SessionStorage,
    goal_id: str,
    status: str,
    *,
    timeout: float = 5.0,
) -> None:
    async def predicate() -> bool:
        goal = await storage.get_goal_run(goal_id)
        return goal is not None and goal.status == status

    assert await _wait_until(predicate, timeout=timeout), (
        f"goal {goal_id} did not reach {status!r}"
    )


async def _wait_for_plan_paused(
    storage: SessionStorage,
    run_id: str,
    pause_reason: str,
    *,
    timeout: float = 5.0,
) -> None:
    async def predicate() -> bool:
        run = await storage.get_plan_run(run_id)
        return (
            run is not None
            and run.status == "paused"
            and run.pause_reason == pause_reason
        )

    assert await _wait_until(predicate, timeout=timeout), (
        f"plan run {run_id} did not pause with {pause_reason!r}"
    )


async def _start_goal(stack: _GoalDriverStack, goal_text: str = "Ship the goal mode.") -> dict:
    response = await _handle_goals_set(
        {
            "sessionKey": SOURCE_KEY,
            "message": goal_text,
            "clientRequestId": "driver-goal-set",
        },
        stack.context,
    )
    terminal = await stack.runtime.wait(response["turnId"], timeout=2.0)
    assert terminal.status == AgentTaskStatus.SUCCEEDED
    return response


# ── Watcher registry unit behavior ────────────────────────────────────────


def test_goal_watcher_registry_observe_unobserve_has_watchers() -> None:
    registry = GoalWatcherRegistry()
    key = "agent:main:webchat:goal-watcher-unittest"
    try:
        assert registry.has_watchers(key) is False
        assert registry.watcher_count(key) == 0

        assert registry.observe(key, "client-a") == 1
        assert registry.has_watchers(key) is True
        assert registry.watcher_count(key) == 1

        # A second observer joins the same session.
        assert registry.observe(key, "client-b") == 2
        assert registry.watcher_count(key) == 2

        # Observing twice with the same client id stays idempotent.
        assert registry.observe(key, "client-b") == 2

        assert registry.unobserve(key, "client-a") == 1
        assert registry.has_watchers(key) is True
        assert registry.unobserve(key, "client-b") == 0
        assert registry.has_watchers(key) is False
        assert registry.watcher_count(key) == 0

        # Unobserved sessions and unknown clients are no-ops.
        assert registry.unobserve(key, "client-a") == 0
        assert registry.unobserve("agent:main:webchat:never-observed", "x") == 0
        assert registry.has_watchers("agent:main:webchat:never-observed") is False

        # Sessions are independent.
        assert registry.observe(key, "client-a") == 1
        assert registry.has_watchers("agent:main:webchat:other-session") is False

        with pytest.raises(ValueError):
            registry.observe(key, "")
    finally:
        registry.unobserve(key, "client-a")
        registry.unobserve(key, "client-b")


# ── Driver scenarios ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_goal_driver_continue_marker_enqueues_next_goal_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    script = _MarkerScript(["[goal:continue]", "[goal:complete]"])
    script.block_turn_index = 2
    script.block_started = asyncio.Event()
    script.block_release = asyncio.Event()
    async with _open_goal_driver_stack(
        tmp_path / "driver-continue.sqlite",
        handler=script,
        goal_config=GoalConfig(continue_unwatched=True),
    ) as stack:
        script.manager = stack.manager
        response = await _start_goal(stack)
        goal_id = response["goalId"]
        run_id = response["planRun"]["runId"]

        # Turn 1 ended with [goal:continue]; the driver auto-enqueued turn 2.
        # Park turn 2 in-flight so the plan-run claim is observable.
        await asyncio.wait_for(script.block_started.wait(), timeout=5.0)
        assert len(script.captured) == 2
        continued = script.captured[1]
        assert continued.run_kind == "goal_turn"
        assert continued.message == GOAL_CONTINUATION_MESSAGE
        assert continued.envelope.metadata["plan_run_id"] == run_id

        # The paused run is re-claimed by the new task while it is live.
        plan_run = await stack.storage.get_plan_run(run_id)
        assert plan_run is not None
        assert plan_run.status == "running"
        assert plan_run.active_task_id == continued.task_id

        # The follow-up task's durable metadata keeps the plan binding but
        # must not inherit the finished task's stale ``task_id``.
        continued_record = await stack.storage.get_agent_task(continued.task_id)
        assert continued_record is not None
        assert continued_record.run_kind == "goal_turn"
        metadata = continued_record.details["metadata"]
        assert metadata["plan_run_id"] == run_id
        assert "task_id" not in metadata

        script.block_release.set()
        await _wait_for_goal_status(stack.storage, goal_id, "complete")
        goal = await stack.storage.get_goal_run(goal_id)
        assert goal is not None
        assert goal.turns == 2
        assert goal.terminal_reason is None
        final_run = await stack.storage.get_plan_run(run_id)
        assert final_run is not None
        assert final_run.status == "cancelled"
        assert final_run.terminal_reason == "goal_complete"
        assert len(script.captured) == 2


@pytest.mark.asyncio
async def test_goal_driver_complete_marker_finishes_goal_and_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    script = _MarkerScript(["[goal:continue]", "[goal:complete]"])
    async with _open_goal_driver_stack(
        tmp_path / "driver-complete.sqlite",
        handler=script,
        goal_config=GoalConfig(continue_unwatched=True),
    ) as stack:
        script.manager = stack.manager
        response = await _start_goal(stack)
        goal_id = response["goalId"]
        run_id = response["planRun"]["runId"]

        # Turn 2 ends with [goal:complete]: the goal and its plan run both
        # terminalize and no third task is ever enqueued.
        await _wait_for_goal_status(stack.storage, goal_id, "complete")
        goal = await stack.storage.get_goal_run(goal_id)
        assert goal is not None
        assert goal.turns == 2
        assert goal.status == "complete"
        assert goal.terminal_reason is None
        assert goal.finished_at is not None

        plan_run = await stack.storage.get_plan_run(run_id)
        assert plan_run is not None
        assert plan_run.status == "cancelled"
        assert plan_run.terminal_reason == "goal_complete"
        assert plan_run.active_task_id is None
        assert len(script.captured) == 2


@pytest.mark.asyncio
async def test_goal_driver_blocks_after_three_same_cause_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    script = _MarkerScript(["[goal:blocked:same-reason]"] * 3)
    async with _open_goal_driver_stack(
        tmp_path / "driver-blocked.sqlite",
        handler=script,
        goal_config=GoalConfig(continue_unwatched=True),
    ) as stack:
        script.manager = stack.manager
        response = await _start_goal(stack)
        goal_id = response["goalId"]
        run_id = response["planRun"]["runId"]

        # Three consecutive same-cause blocked markers hit the retry ceiling:
        # the goal blocks after the third turn and the loop stops.
        await _wait_for_goal_status(stack.storage, goal_id, "blocked")
        goal = await stack.storage.get_goal_run(goal_id)
        assert goal is not None
        assert goal.status == "blocked"
        assert goal.turns == 3
        assert goal.blocked_reason == "same-reason"
        assert goal.blocked_retries == 3
        assert goal.terminal_reason == "blocked_after_retries:same-reason"

        plan_run = await stack.storage.get_plan_run(run_id)
        assert plan_run is not None
        assert plan_run.status == "cancelled"
        assert plan_run.terminal_reason == "goal_blocked"
        assert plan_run.active_task_id is None
        assert len(script.captured) == 3


@pytest.mark.asyncio
async def test_goal_driver_max_turns_blocks_after_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    script = _MarkerScript(["[goal:continue]", "[goal:continue]"])
    async with _open_goal_driver_stack(
        tmp_path / "driver-max-turns.sqlite",
        handler=script,
        goal_config=GoalConfig(max_turns=2, continue_unwatched=True),
    ) as stack:
        script.manager = stack.manager
        response = await _start_goal(stack)
        goal_id = response["goalId"]
        run_id = response["planRun"]["runId"]

        # Two continue turns reach max_turns=2: the second turn's hook blocks
        # the run instead of enqueueing a third task.
        await _wait_for_goal_status(stack.storage, goal_id, "blocked")
        goal = await stack.storage.get_goal_run(goal_id)
        assert goal is not None
        assert goal.status == "blocked"
        assert goal.turns == 2
        assert goal.terminal_reason == "goal_continuation_limit_reached"

        plan_run = await stack.storage.get_plan_run(run_id)
        assert plan_run is not None
        assert plan_run.status == "cancelled"
        assert plan_run.terminal_reason == "goal_blocked"
        assert len(script.captured) == 2


@pytest.mark.asyncio
async def test_goal_driver_gates_continuation_on_watchers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    registry = get_goal_watcher_registry()
    registry.unobserve(SOURCE_KEY, _WATCHER_CLIENT)  # defensive cleanup
    script = _MarkerScript(["[goal:continue]", "[goal:complete]"])
    try:
        async with _open_goal_driver_stack(
            tmp_path / "driver-watcher.sqlite",
            handler=script,
            goal_config=GoalConfig(),  # continue_unwatched=False
        ) as stack:
            script.manager = stack.manager
            response = await _start_goal(stack)
            goal_id = response["goalId"]
            run_id = response["planRun"]["runId"]

            # No observer: the driver parks the loop. The plan run stays
            # paused at the goal_turn_finished anchor and the goal stays
            # running; nothing is enqueued.
            await _wait_for_plan_paused(
                stack.storage, run_id, "goal_turn_finished"
            )
            await asyncio.sleep(0.05)  # give a (buggy) enqueue a chance
            assert len(script.captured) == 1
            goal = await stack.storage.get_goal_run(goal_id)
            assert goal is not None
            assert goal.status == "running"

            # Once a watcher appears, a driver pass on the same paused anchor
            # continues the loop.
            registry.observe(SOURCE_KEY, _WATCHER_CLIENT)
            turn1 = script.captured[0]
            handle = await maybe_continue_goal(
                stack.runtime,
                SimpleNamespace(envelope=turn1.envelope, task_id=turn1.task_id),
                config=GoalConfig(),
            )
            assert handle is not None
            continued_task_id = str(getattr(handle, "task_id", "") or "")
            assert continued_task_id

            await _wait_for_goal_status(stack.storage, goal_id, "complete")
            assert len(script.captured) == 2
            continued_record = await stack.storage.get_agent_task(
                continued_task_id
            )
            assert continued_record is not None
            assert continued_record.run_kind == "goal_turn"
            metadata = continued_record.details["metadata"]
            assert metadata["plan_run_id"] == run_id
            assert "task_id" not in metadata
    finally:
        registry.unobserve(SOURCE_KEY, _WATCHER_CLIENT)


@pytest.mark.asyncio
async def test_goal_driver_idle_turns_inject_progress_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.gateway.rpc_sessions._emit_to_subscribers",
        _ignore_subscriber_event,
    )
    script = _MarkerScript(
        ["Working without a marker.", "Still no marker.", "[goal:complete]"]
    )
    async with _open_goal_driver_stack(
        tmp_path / "driver-idle.sqlite",
        handler=script,
        goal_config=GoalConfig(idle_turns=2, continue_unwatched=True),
    ) as stack:
        script.manager = stack.manager
        response = await _start_goal(stack)
        goal_id = response["goalId"]

        # Two consecutive no-marker turns cross the idle threshold: the third
        # auto-enqueued turn carries the nudge prompt, then the script's
        # terminal marker finishes the loop.
        await _wait_for_goal_status(stack.storage, goal_id, "complete")
        assert len(script.captured) == 3
        nudge = script.captured[2]
        assert nudge.run_kind == "goal_turn"
        assert nudge.message.startswith(GOAL_CONTINUATION_MESSAGE)
        assert IDLE_PROGRESS_PROMPT in nudge.message

        goal = await stack.storage.get_goal_run(goal_id)
        assert goal is not None
        assert goal.status == "complete"
        assert goal.turns == 3
        assert goal.idle_turns == 0  # counter reset at the nudge injection
