from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import pytest

from opensquilla.orchestration.executor import (
    ActivationExecutionResult,
    OrchestrationExecutor,
)
from opensquilla.orchestration.models import (
    ActivationPhase,
    AgentActivationRecord,
    AgentState,
    OrchestrationMode,
    TaskOutcome,
)
from opensquilla.orchestration.repository import OrchestrationRepository
from opensquilla.orchestration.service import (
    DelegateDisposition,
    DelegateRequest,
    OrchestrationService,
)
from opensquilla.orchestration.state import derive_agent_state

pytestmark = pytest.mark.asyncio


def _id_factory() -> Callable[[str], str]:
    counters: dict[str, int] = {}

    def allocate(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]}"

    return allocate


@dataclass
class ControlledRunner:
    gate: asyncio.Event
    started: list[str]
    started_signal: asyncio.Event
    interrupted: list[str] = field(default_factory=list)

    async def run(self, *, activation, session, task, route) -> ActivationExecutionResult:
        self.started.append(activation.activation_id)
        self.started_signal.set()
        await self.gate.wait()
        return ActivationExecutionResult(
            outcome=TaskOutcome.SUCCEEDED,
            result={"summary": task.description, "route": route},
        )

    async def interrupt(self, *, activation, reason: str) -> None:
        del reason
        self.interrupted.append(activation.activation_id)


@pytest.fixture
async def executor(tmp_path):
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement the requested change",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=frozenset({"read_file", "delegate_task", "interrupt_agent"}),
        registered_tools=frozenset({"read_file", "delegate_task", "interrupt_agent"}),
    )
    gate = asyncio.Event()
    runner = ControlledRunner(gate=gate, started=[], started_signal=asyncio.Event())
    route_calls: list[tuple[str, str]] = []

    async def route_child(task, session) -> dict[str, str]:
        route_calls.append((task.task_id, session.session_id))
        return {"model": "tree-selected-model"}

    orchestration_executor = OrchestrationExecutor(
        service,
        runner=runner,
        route_child=route_child,
    )
    try:
        yield orchestration_executor, gate, runner, route_calls
    finally:
        await orchestration_executor.close()
        await repo.close()


def _request(
    *,
    task_key: str,
    background: bool,
    session_id: str | None = None,
    parent_activation_id: str | None = None,
) -> DelegateRequest:
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    return DelegateRequest(
        run_id="run-1",
        parent_session_id="root-session",
        parent_task_id="root-task",
        task_key=task_key,
        task=f"Perform {task_key}",
        acceptance_criteria=f"Return the complete {task_key} result with evidence",
        inherited_tools=tools,
        registered_tools=tools,
        background=background,
        session_id=session_id,
        parent_activation_id=parent_activation_id,
    )


async def test_foreground_is_default_and_blocks_until_child_finishes(executor) -> None:
    orchestration_executor, gate, runner, _route_calls = executor

    foreground = asyncio.create_task(
        orchestration_executor.delegate(_request(task_key="foreground", background=False))
    )
    await asyncio.wait_for(runner.started_signal.wait(), timeout=1)

    assert not foreground.done()
    assert len(runner.started) == 1
    activation = await orchestration_executor.service.repository.get_activation(runner.started[0])
    assert activation is not None
    assert activation.phase is ActivationPhase.RUNNING

    gate.set()
    result = await foreground
    assert result.result == {
        "summary": "Perform foreground",
        "route": {"model": "tree-selected-model"},
    }


async def test_foreground_publishes_child_progress_before_unblocking_parent(executor) -> None:
    orchestration_executor, gate, runner, _route_calls = executor
    updates: list[tuple[str, str, str]] = []

    async def publish(run_id: str, task_id: str, phase: str) -> None:
        updates.append((run_id, task_id, phase))

    orchestration_executor.progress_notifier = publish
    foreground = asyncio.create_task(
        orchestration_executor.delegate(_request(task_key="progress", background=False))
    )
    await asyncio.wait_for(runner.started_signal.wait(), timeout=1)

    assert not foreground.done()
    assert [phase for _run, _task, phase in updates] == ["waiting", "working"]

    gate.set()
    await foreground
    assert [phase for _run, _task, phase in updates] == [
        "waiting", "working", "completed"
    ]
    assert all(run_id == "run-1" for run_id, _task, _phase in updates)
    assert len({task_id for _run, task_id, _phase in updates}) == 1


async def test_foreground_wait_uses_runtime_slot_yield_scope(executor) -> None:
    orchestration_executor, gate, runner, _route_calls = executor
    transitions: list[str] = []

    @asynccontextmanager
    async def foreground_wait_scope():
        transitions.append("released")
        try:
            yield
        finally:
            transitions.append("reacquired")

    orchestration_executor._foreground_wait_scope = foreground_wait_scope
    foreground = asyncio.create_task(
        orchestration_executor.delegate(_request(task_key="yield-slot", background=False))
    )
    await asyncio.wait_for(runner.started_signal.wait(), timeout=1)

    assert transitions == ["released"]
    gate.set()
    await foreground
    assert transitions == ["released", "reacquired"]


async def test_cancelling_foreground_delegation_interrupts_child_and_queue(executor) -> None:
    orchestration_executor, _gate, runner, _route_calls = executor
    foreground = asyncio.create_task(
        orchestration_executor.delegate(_request(task_key="cancelled", background=False))
    )
    await asyncio.wait_for(runner.started_signal.wait(), timeout=1)
    task = await orchestration_executor.service.repository.find_task_by_key("run-1", "cancelled")
    assert task is not None
    activation = await orchestration_executor.service.repository.latest_activation_for_task(
        task.task_id
    )
    assert activation is not None
    queued = await orchestration_executor.service.delegate(
        _request(
            task_key="never-run",
            background=False,
            session_id=task.owner_session_id,
        )
    )

    foreground.cancel()
    with pytest.raises(asyncio.CancelledError):
        await foreground

    terminal = await orchestration_executor.service.repository.get_task(task.task_id)
    queued_terminal = await orchestration_executor.service.repository.get_task(queued.task.task_id)
    assert runner.interrupted == [activation.activation_id]
    assert terminal is not None and terminal.outcome is TaskOutcome.INTERRUPTED
    assert queued_terminal is not None
    assert queued_terminal.outcome is TaskOutcome.INTERRUPTED
    assert (
        await orchestration_executor.service.repository.current_activation_for_session(
            task.owner_session_id
        )
        is None
    )


async def test_foreground_cancel_interrupts_child_before_reacquiring_parent_slot(
    executor,
) -> None:
    orchestration_executor, _gate, runner, _route_calls = executor
    child_interrupted = asyncio.Event()

    original_interrupt = runner.interrupt

    async def observe_interrupt(*, activation, reason: str) -> None:
        await original_interrupt(activation=activation, reason=reason)
        child_interrupted.set()

    @asynccontextmanager
    async def parent_slot_scope():
        try:
            yield
        finally:
            await child_interrupted.wait()

    runner.interrupt = observe_interrupt  # type: ignore[method-assign]
    orchestration_executor._foreground_wait_scope = parent_slot_scope
    foreground = asyncio.create_task(
        orchestration_executor.delegate(
            _request(task_key="cancel-before-reacquire", background=False)
        )
    )
    await asyncio.wait_for(runner.started_signal.wait(), timeout=1)

    foreground.cancel()
    await asyncio.sleep(0.05)
    try:
        assert foreground.done(), "child interruption must precede parent slot reacquisition"
    finally:
        child_interrupted.set()
        with suppress(asyncio.CancelledError):
            await foreground


async def test_cancelling_during_parent_wait_setup_interrupts_persisted_child(executor) -> None:
    orchestration_executor, _gate, runner, _route_calls = executor
    mark_started = asyncio.Event()

    async def blocked_mark_parent_waiting(*, request, child_activation_id):
        del request, child_activation_id
        mark_started.set()
        await asyncio.Event().wait()

    orchestration_executor._mark_parent_waiting = blocked_mark_parent_waiting
    foreground = asyncio.create_task(
        orchestration_executor.delegate(_request(task_key="cancel-during-setup", background=False))
    )
    await asyncio.wait_for(mark_started.wait(), timeout=1)
    task = await orchestration_executor.service.repository.find_task_by_key(
        "run-1", "cancel-during-setup"
    )
    assert task is not None
    activation = await orchestration_executor.service.repository.latest_activation_for_task(
        task.task_id
    )
    assert activation is not None

    foreground.cancel()
    with pytest.raises(asyncio.CancelledError):
        await foreground

    terminal = await orchestration_executor.service.repository.get_task(task.task_id)
    assert runner.interrupted == [activation.activation_id]
    assert terminal is not None and terminal.outcome is TaskOutcome.INTERRUPTED


async def test_success_racing_with_interrupt_cannot_promote_queued_work(executor) -> None:
    orchestration_executor, gate, _runner, _route_calls = executor

    class SlowInterruptRunner(ControlledRunner):
        def __init__(self) -> None:
            super().__init__(gate=gate, started=[], started_signal=asyncio.Event())
            self.interrupt_started = asyncio.Event()
            self.interrupt_release = asyncio.Event()

        async def interrupt(self, *, activation, reason: str) -> None:
            del reason
            self.interrupted.append(activation.activation_id)
            self.interrupt_started.set()
            await self.interrupt_release.wait()

    runner = SlowInterruptRunner()
    orchestration_executor.runner = runner
    first = await orchestration_executor.delegate(_request(task_key="racing", background=True))
    await asyncio.wait_for(runner.started_signal.wait(), timeout=1)
    queued = await orchestration_executor.service.delegate(
        _request(
            task_key="must-not-start",
            background=False,
            session_id=first.session_id,
        )
    )
    interrupting = asyncio.create_task(
        orchestration_executor.interrupt(
            caller_session_id="root-session",
            session_id=first.session_id,
            activation_id=first.activation_id,
            reason="stop racing task",
        )
    )
    await asyncio.wait_for(runner.interrupt_started.wait(), timeout=1)

    gate.set()
    while True:
        terminal = await orchestration_executor.service.repository.get_task(first.task_id)
        if terminal is not None and terminal.outcome is not TaskOutcome.PENDING:
            break
        await asyncio.sleep(0)
    queued_terminal = await orchestration_executor.service.repository.get_task(queued.task.task_id)
    assert terminal.outcome is TaskOutcome.INTERRUPTED
    assert queued_terminal is not None
    assert queued_terminal.outcome is TaskOutcome.INTERRUPTED
    assert (
        await orchestration_executor.service.repository.current_activation_for_session(
            first.session_id
        )
        is None
    )

    runner.interrupt_release.set()
    await asyncio.wait_for(interrupting, timeout=1)


async def test_foreground_retry_waits_for_the_new_attempt(tmp_path) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )

    class RetryRunner:
        def __init__(self) -> None:
            self.gates = [asyncio.Event(), asyncio.Event()]
            self.started = 0

        async def run(self, *, activation, session, task, route):
            del activation, session, task, route
            attempt = self.started
            self.started += 1
            await self.gates[attempt].wait()
            if attempt == 0:
                return ActivationExecutionResult(
                    outcome=TaskOutcome.FAILED,
                    result={"error": "first attempt"},
                )
            return ActivationExecutionResult(
                outcome=TaskOutcome.SUCCEEDED,
                result={"summary": "second attempt"},
            )

        async def interrupt(self, *, activation, reason):
            del activation, reason

    runner = RetryRunner()

    async def route_child(task, session):
        del task, session
        return None

    orchestration_executor = OrchestrationExecutor(
        service,
        runner=runner,
        route_child=route_child,
    )
    try:
        first = asyncio.create_task(
            orchestration_executor.delegate(_request(task_key="retry", background=False))
        )
        while runner.started < 1:
            await asyncio.sleep(0)
        runner.gates[0].set()
        assert (await first).result == {
            "status": "failed",
            "summary": "first attempt",
            "error": "first attempt",
            "retry_same_agent": True,
        }

        retry = asyncio.create_task(
            orchestration_executor.delegate(_request(task_key="retry", background=False))
        )
        while runner.started < 2:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not retry.done()
        runner.gates[1].set()
        assert (await retry).result == {"summary": "second attempt"}
    finally:
        await orchestration_executor.close()
        await repo.close()


async def test_retry_does_not_share_waiter_with_old_attempt_blocked_in_notification(
    tmp_path,
) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )

    class RetryRunner:
        def __init__(self) -> None:
            self.gates = [asyncio.Event(), asyncio.Event()]
            self.started = 0

        async def run(self, *, activation, session, task, route):
            del activation, session, task, route
            attempt = self.started
            self.started += 1
            await self.gates[attempt].wait()
            if attempt == 0:
                return ActivationExecutionResult(
                    outcome=TaskOutcome.FAILED,
                    result={"error": "old attempt"},
                )
            return ActivationExecutionResult(
                outcome=TaskOutcome.SUCCEEDED,
                result={"summary": "new attempt"},
            )

        async def interrupt(self, *, activation, reason):
            del activation, reason

    notification_entered = asyncio.Event()
    release_notification = asyncio.Event()

    async def blocked_notifier(**kwargs) -> None:
        del kwargs
        notification_entered.set()
        await release_notification.wait()

    async def no_route(task, session):
        del task, session
        return None

    runner = RetryRunner()
    orchestration_executor = OrchestrationExecutor(
        service,
        runner=runner,
        route_child=no_route,
        notify_parent=blocked_notifier,
    )
    try:
        first = await orchestration_executor.delegate(
            _request(task_key="retry-race", background=True)
        )
        while runner.started < 1:
            await asyncio.sleep(0)
        runner.gates[0].set()
        await asyncio.wait_for(notification_entered.wait(), timeout=1)

        failed = await repo.get_task(first.task_id)
        assert failed is not None and failed.outcome is TaskOutcome.FAILED

        retry = asyncio.create_task(
            orchestration_executor.delegate(_request(task_key="retry-race", background=False))
        )
        while runner.started < 2:
            await asyncio.sleep(0)
        assert await repo.list_unprocessed_child_result_messages() == []
        release_notification.set()
        await asyncio.sleep(0)
        assert not retry.done()

        runner.gates[1].set()
        assert (await asyncio.wait_for(retry, timeout=1)).result == {"summary": "new attempt"}
    finally:
        release_notification.set()
        for gate in runner.gates:
            gate.set()
        await orchestration_executor.close()
        await repo.close()


async def test_orphaned_activation_releases_foreground_waiter_and_runtime(executor) -> None:
    orchestration_executor, _gate, runner, _route_calls = executor
    foreground = asyncio.create_task(
        orchestration_executor.delegate(_request(task_key="expired", background=False))
    )
    await asyncio.wait_for(runner.started_signal.wait(), timeout=1)
    task = await orchestration_executor.service.repository.find_task_by_key("run-1", "expired")
    assert task is not None
    activation = await orchestration_executor.service.repository.latest_activation_for_task(
        task.task_id
    )
    assert activation is not None

    await orchestration_executor.recover_orphaned_activation(activation)

    result = await asyncio.wait_for(foreground, timeout=1)
    terminal = await orchestration_executor.service.repository.get_task(task.task_id)
    assert result.result == {
        "status": "interrupted",
        "summary": "gateway_restarted",
        "error": "gateway_restarted",
        "retry_same_agent": True,
        "reason": "gateway_restarted",
        "recoverable": True,
    }
    assert terminal is not None and terminal.outcome is TaskOutcome.INTERRUPTED


async def test_expired_stopping_activation_interrupts_queued_follow_up(tmp_path) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    first = await service.delegate(_request(task_key="active", background=True))
    claimed = await repo.claim_activation(first.activation.activation_id)
    assert claimed is not None
    queued = await service.delegate(
        _request(
            task_key="queued",
            background=False,
            session_id=first.session.session_id,
        )
    )
    stopping = (
        await service.request_interrupt(
            caller_session_id="root-session",
            session_id=first.session.session_id,
            activation_id=first.activation.activation_id,
            reason="stop after restart",
        )
    )[0]
    runner = ControlledRunner(
        gate=asyncio.Event(),
        started=[],
        started_signal=asyncio.Event(),
    )

    async def no_route(task, session):
        del task, session
        return None

    orchestration_executor = OrchestrationExecutor(
        service,
        runner=runner,
        route_child=no_route,
    )
    try:
        await orchestration_executor.recover_orphaned_activation(stopping)

        first_task = await repo.get_task(first.task.task_id)
        queued_task = await repo.get_task(queued.task.task_id)
        assert first_task is not None and first_task.outcome is TaskOutcome.INTERRUPTED
        assert queued_task is not None and queued_task.outcome is TaskOutcome.INTERRUPTED
        assert await repo.current_activation_for_session(first.session.session_id) is None
        assert runner.started == []
    finally:
        await orchestration_executor.close()
        await repo.close()


async def test_background_returns_before_child_finishes(executor) -> None:
    orchestration_executor, _gate, runner, _route_calls = executor

    result = await orchestration_executor.delegate(_request(task_key="background", background=True))

    assert result.result is None
    assert len(runner.started) <= 1


async def test_new_child_calls_tree_router_exactly_once(executor) -> None:
    orchestration_executor, gate, _runner, route_calls = executor

    task = asyncio.create_task(
        orchestration_executor.delegate(_request(task_key="routed", background=False))
    )
    await asyncio.wait_for(_runner.started_signal.wait(), timeout=1)
    assert len(route_calls) == 1

    gate.set()
    await task
    assert len(route_calls) == 1


async def test_interrupt_queued_activation_releases_without_overbooking(tmp_path) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    children = [
        await service.delegate(_request(task_key=f"task-{index}", background=True))
        for index in range(9)
    ]
    runner = ControlledRunner(
        gate=asyncio.Event(),
        started=[],
        started_signal=asyncio.Event(),
    )

    async def route_child(task, session) -> None:
        return None

    orchestration_executor = OrchestrationExecutor(
        service,
        runner=runner,
        route_child=route_child,
    )
    try:
        queued = children[-1]
        assert queued.activation.phase is ActivationPhase.QUEUED

        await orchestration_executor.interrupt(
            caller_session_id="root-session",
            session_id=queued.session.session_id,
            activation_id=queued.activation.activation_id,
            reason="no longer needed",
        )

        activation = await repo.get_activation(queued.activation.activation_id)
        task = await repo.get_task(queued.task.task_id)
        assert activation is not None
        assert activation.phase is ActivationPhase.RELEASED
        assert task is not None
        assert task.outcome is TaskOutcome.INTERRUPTED
        assert await repo.live_direct_child_count("root-session") == 8
        assert await repo.oldest_queued_direct_child("root-session") is None
    finally:
        await orchestration_executor.close()
        await repo.close()


async def test_interrupt_releases_activation_and_interrupts_queued_session_work(
    executor,
) -> None:
    orchestration_executor, _gate, runner, _route_calls = executor
    first = await orchestration_executor.delegate(_request(task_key="first", background=True))
    await asyncio.wait_for(runner.started_signal.wait(), timeout=1)
    queued = await orchestration_executor.delegate(
        _request(
            task_key="queued-follow-up",
            background=True,
            session_id=first.session_id,
        )
    )

    await orchestration_executor.interrupt(
        caller_session_id="root-session",
        session_id=first.session_id,
        activation_id=first.activation_id,
        reason="stop the whole worker",
    )
    for _ in range(100):
        current = await orchestration_executor.service.repository.current_activation_for_session(
            first.session_id
        )
        if current is None:
            break
        await asyncio.sleep(0.01)

    first_task = await orchestration_executor.service.repository.get_task(first.task_id)
    queued_task = await orchestration_executor.service.repository.get_task(queued.task_id)
    assert first_task is not None and first_task.outcome is TaskOutcome.INTERRUPTED
    assert queued_task is not None and queued_task.outcome is TaskOutcome.INTERRUPTED
    assert (
        await orchestration_executor.service.repository.current_activation_for_session(
            first.session_id
        )
        is None
    )


async def test_interrupt_times_out_a_stuck_runtime_cancel(executor) -> None:
    orchestration_executor, _gate, runner, _route_calls = executor
    delegated = await orchestration_executor.delegate(_request(task_key="stuck", background=True))
    await asyncio.wait_for(runner.started_signal.wait(), timeout=1)

    async def stuck_interrupt(*, activation, reason: str) -> None:
        await asyncio.Event().wait()

    runner.interrupt = stuck_interrupt  # type: ignore[method-assign]
    orchestration_executor._interrupt_timeout_seconds = 0.01

    await asyncio.wait_for(
        orchestration_executor.interrupt(
            caller_session_id="root-session",
            session_id=delegated.session_id,
            activation_id=delegated.activation_id,
            reason="cancel stuck runtime",
        ),
        timeout=0.5,
    )


async def test_nested_foreground_delegate_marks_parent_waiting_then_working(executor) -> None:
    orchestration_executor, gate, runner, _route_calls = executor
    repo = orchestration_executor.service.repository
    await repo.create_activation(
        AgentActivationRecord(
            activation_id="root-activation",
            session_id="root-session",
            task_id="root-task",
            phase=ActivationPhase.RUNNING,
            live_model_call=True,
        )
    )

    foreground = asyncio.create_task(
        orchestration_executor.delegate(
            _request(
                task_key="nested-foreground",
                background=False,
                parent_activation_id="root-activation",
            )
        )
    )
    await asyncio.wait_for(runner.started_signal.wait(), timeout=1)

    parent = await repo.current_activation_for_session("root-session")
    root_session = await repo.get_session("root-session")
    assert parent is not None and root_session is not None
    assert parent.live_model_call is False
    assert (
        derive_agent_state(
            root_session,
            parent,
            inbox_count=0,
            live_descendants=await repo.live_descendant_count("root-session"),
        )
        is AgentState.WAITING_CHILDREN
    )

    gate.set()
    await foreground
    restored = await repo.current_activation_for_session("root-session")
    assert restored is not None
    assert restored.live_model_call is True


async def test_parallel_foreground_waits_restore_parent_only_after_last_child(
    executor,
) -> None:
    orchestration_executor, _gate, runner, _route_calls = executor
    repo = orchestration_executor.service.repository
    await repo.create_activation(
        AgentActivationRecord(
            activation_id="root-activation",
            session_id="root-session",
            task_id="root-task",
            phase=ActivationPhase.RUNNING,
            live_model_call=True,
        )
    )
    gates = {
        "parallel-a": asyncio.Event(),
        "parallel-b": asyncio.Event(),
    }
    activation_by_key: dict[str, str] = {}

    async def run_parallel(*, activation, session, task, route):
        del session, route
        activation_by_key[task.task_key] = activation.activation_id
        await gates[task.task_key].wait()
        return ActivationExecutionResult(
            outcome=TaskOutcome.SUCCEEDED,
            result={"summary": task.task_key},
        )

    runner.run = run_parallel  # type: ignore[method-assign]
    foreground_a = asyncio.create_task(
        orchestration_executor.delegate(
            _request(
                task_key="parallel-a",
                background=False,
                parent_activation_id="root-activation",
            )
        )
    )
    foreground_b = asyncio.create_task(
        orchestration_executor.delegate(
            _request(
                task_key="parallel-b",
                background=False,
                parent_activation_id="root-activation",
            )
        )
    )
    async with asyncio.timeout(1):
        while len(activation_by_key) != 2:
            await asyncio.sleep(0)

    gates["parallel-a"].set()
    await foreground_a
    while_a_finished = await repo.current_activation_for_session("root-session")
    try:
        assert while_a_finished is not None
        assert while_a_finished.live_model_call is False
        assert while_a_finished.external_wait_id == activation_by_key["parallel-b"]
    finally:
        gates["parallel-b"].set()
        await foreground_b
    all_finished = await repo.current_activation_for_session("root-session")
    assert all_finished is not None
    assert all_finished.live_model_call is True
    assert all_finished.external_wait_id is None


async def test_foreground_append_waits_behind_running_background_session(
    executor,
) -> None:
    orchestration_executor, gate, runner, _route_calls = executor
    repo = orchestration_executor.service.repository
    await repo.create_activation(
        AgentActivationRecord(
            activation_id="root-activation",
            session_id="root-session",
            task_id="root-task",
            phase=ActivationPhase.RUNNING,
            live_model_call=True,
        )
    )

    background = await orchestration_executor.delegate(
        _request(
            task_key="background-active",
            background=True,
            parent_activation_id="root-activation",
        )
    )
    await asyncio.wait_for(runner.started_signal.wait(), timeout=1)
    foreground = asyncio.create_task(
        orchestration_executor.delegate(
            _request(
                task_key="foreground-appended",
                background=False,
                session_id=background.session_id,
                parent_activation_id="root-activation",
            )
        )
    )
    async with asyncio.timeout(1):
        while (await repo.find_task_by_key("run-1", "foreground-appended")) is None:
            await asyncio.sleep(0)

    parent = await repo.current_activation_for_session("root-session")
    appended = await repo.find_task_by_key("run-1", "foreground-appended")
    assert parent is not None and appended is not None
    assert not foreground.done()
    assert parent.live_model_call is False
    assert parent.external_wait_id == appended.task_id

    gate.set()
    await foreground
    restored = await repo.current_activation_for_session("root-session")
    assert restored is not None
    assert restored.live_model_call is True
    assert restored.external_wait_id is None


async def test_completed_task_reuse_does_not_route_or_execute_again(executor) -> None:
    orchestration_executor, gate, runner, route_calls = executor
    first = asyncio.create_task(
        orchestration_executor.delegate(_request(task_key="once", background=False))
    )
    await asyncio.wait_for(runner.started_signal.wait(), timeout=1)
    gate.set()
    completed = await first

    reused = await orchestration_executor.delegate(_request(task_key="once", background=False))

    assert reused.result == completed.result
    assert len(route_calls) == 1
    assert len(runner.started) == 1


async def test_route_failure_settles_foreground_without_hanging(tmp_path) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    runner = ControlledRunner(
        gate=asyncio.Event(),
        started=[],
        started_signal=asyncio.Event(),
    )

    async def broken_route(task, session) -> Any:
        raise RuntimeError("router exploded")

    orchestration_executor = OrchestrationExecutor(
        service,
        runner=runner,
        route_child=broken_route,
    )
    try:
        result = await asyncio.wait_for(
            orchestration_executor.delegate(_request(task_key="route-failure", background=False)),
            timeout=1,
        )
        assert result.result == {
            "status": "failed",
            "summary": "router exploded",
            "error": "router exploded",
            "error_type": "RuntimeError",
            "retry_same_agent": True,
        }
        task = await repo.get_task(result.task_id)
        assert task is not None
        assert task.outcome is TaskOutcome.FAILED
    finally:
        await orchestration_executor.close()
        await repo.close()


async def test_recovery_routes_persisted_initial_activation(tmp_path) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    persisted = await service.delegate(_request(task_key="recover-route", background=True))
    gate = asyncio.Event()
    gate.set()
    runner = ControlledRunner(gate=gate, started=[], started_signal=asyncio.Event())
    route_calls: list[str] = []

    async def route_child(task, session):
        del session
        route_calls.append(task.task_id)
        return {"model": "recovered-tree-model"}

    recovered = OrchestrationExecutor(service, runner=runner, route_child=route_child)
    try:
        assert persisted.activation.route_required is True
        await recovered.recover_startable_activations()
        for _ in range(100):
            task = await repo.get_task(persisted.task.task_id)
            if task is not None and task.outcome is TaskOutcome.SUCCEEDED:
                break
            await asyncio.sleep(0.01)
        assert route_calls == [persisted.task.task_id]
        assert len(runner.started) == 1
    finally:
        await recovered.close()
        await repo.close()


async def test_interrupt_settles_when_driver_is_cancelled_during_commit(tmp_path) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )

    class ImmediateRunner:
        async def run(self, *, activation, session, task, route):
            del activation, session, task, route
            return ActivationExecutionResult(
                outcome=TaskOutcome.SUCCEEDED,
                result={"summary": "done"},
            )

        async def interrupt(self, *, activation, reason):
            del activation, reason

    entered_commit = asyncio.Event()
    never_release = asyncio.Event()
    original_settle = service.settle_activation
    settle_calls = 0

    async def block_first_settle(*args, **kwargs):
        nonlocal settle_calls
        settle_calls += 1
        if settle_calls == 1:
            entered_commit.set()
            await never_release.wait()
        return await original_settle(*args, **kwargs)

    service.settle_activation = block_first_settle  # type: ignore[method-assign]

    async def no_route(task, session):
        del task, session
        return None

    executor = OrchestrationExecutor(service, runner=ImmediateRunner(), route_child=no_route)
    try:
        delegated = await executor.delegate(_request(task_key="interrupt-race", background=True))
        await asyncio.wait_for(entered_commit.wait(), timeout=1)
        await asyncio.wait_for(
            executor.interrupt(
                caller_session_id="root-session",
                session_id=delegated.session_id,
                activation_id=delegated.activation_id,
                reason="stop loop",
            ),
            timeout=1,
        )
        activation = await repo.get_activation(delegated.activation_id)
        task = await repo.get_task(delegated.task_id)
        assert activation is not None and activation.phase is ActivationPhase.RELEASED
        assert task is not None and task.outcome is TaskOutcome.INTERRUPTED
    finally:
        never_release.set()
        await executor.close()
        await repo.close()


async def test_background_result_notifies_only_after_durable_settlement(tmp_path) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    gate = asyncio.Event()
    runner = ControlledRunner(gate=gate, started=[], started_signal=asyncio.Event())
    notifications: list[tuple[str, TaskOutcome]] = []
    notified = asyncio.Event()

    async def notify_parent(*, session, task, activation_id, outcome, result) -> None:
        del activation_id
        persisted = await repo.get_task(task.task_id)
        assert persisted is not None and persisted.outcome is outcome
        notifications.append((task.task_id, outcome))
        notified.set()

    async def no_route(task, session) -> Any:
        return None

    orchestration_executor = OrchestrationExecutor(
        service,
        runner=runner,
        route_child=no_route,
        notify_parent=notify_parent,
    )
    try:
        delegated = await orchestration_executor.delegate(
            _request(task_key="background-notify", background=True)
        )
        await asyncio.wait_for(runner.started_signal.wait(), timeout=1)
        assert notifications == []
        gate.set()
        await asyncio.wait_for(notified.wait(), timeout=1)
        assert notifications == [(delegated.task_id, TaskOutcome.SUCCEEDED)]
    finally:
        await orchestration_executor.close()
        await repo.close()


async def test_foreground_reuse_observes_and_cancels_background_delivery(tmp_path) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    gate = asyncio.Event()
    runner = ControlledRunner(gate=gate, started=[], started_signal=asyncio.Event())

    class PendingNotifier:
        def __init__(self) -> None:
            self.sent = asyncio.Event()
            self.cancelled: list[tuple[str, str]] = []

        async def __call__(self, **kwargs) -> bool:
            del kwargs
            self.sent.set()
            return False

        async def cancel_pending_delivery(self, *, task, session, activation_id) -> None:
            del session
            self.cancelled.append((task.task_id, activation_id))

    notifier = PendingNotifier()

    async def no_route(task, session):
        del task, session
        return None

    orchestration_executor = OrchestrationExecutor(
        service,
        runner=runner,
        route_child=no_route,
        notify_parent=notifier,
    )
    try:
        background = await orchestration_executor.delegate(
            _request(task_key="observe", background=True)
        )
        await asyncio.wait_for(runner.started_signal.wait(), timeout=1)
        gate.set()
        await asyncio.wait_for(notifier.sent.wait(), timeout=1)

        reused = await orchestration_executor.delegate(
            _request(task_key="observe", background=False)
        )

        assert reused.disposition is DelegateDisposition.REUSED
        assert reused.result == {
            "summary": "Perform observe",
            "route": None,
        }
        assert notifier.cancelled == [(background.task_id, background.activation_id)]
        assert await repo.list_unprocessed_child_result_messages() == []
    finally:
        gate.set()
        await orchestration_executor.close()
        await repo.close()


async def test_appended_foreground_task_runs_as_its_own_activation(executor) -> None:
    orchestration_executor, gate, runner, _route_calls = executor
    first = await orchestration_executor.delegate(_request(task_key="first", background=True))
    await asyncio.wait_for(runner.started_signal.wait(), timeout=1)

    appended = asyncio.create_task(
        orchestration_executor.delegate(
            _request(
                task_key="follow-up",
                background=False,
                session_id=first.session_id,
            )
        )
    )
    for _ in range(100):
        if (
            await orchestration_executor.service.repository.find_task_by_key("run-1", "follow-up")
            is not None
        ):
            break
        await asyncio.sleep(0.01)
    assert not appended.done()

    gate.set()
    result = await asyncio.wait_for(appended, timeout=1)
    assert result.result == {
        "summary": "Perform follow-up",
        "route": None,
    }
    assert len(runner.started) == 2
    assert len(_route_calls) == 1
    task = await orchestration_executor.service.repository.get_task(result.task_id)
    assert task is not None
    assert task.outcome is TaskOutcome.SUCCEEDED


async def test_foreground_attach_suppresses_background_result_delivery(executor) -> None:
    orchestration_executor, gate, runner, _route_calls = executor
    background = await orchestration_executor.delegate(_request(task_key="shared", background=True))
    await asyncio.wait_for(runner.started_signal.wait(), timeout=1)

    foreground = asyncio.create_task(
        orchestration_executor.delegate(_request(task_key="shared", background=False))
    )
    await asyncio.sleep(0)
    assert not foreground.done()
    gate.set()

    result = await asyncio.wait_for(foreground, timeout=1)
    assert result.disposition is DelegateDisposition.ATTACHED
    assert result.result == {"summary": "Perform shared", "route": {"model": "tree-selected-model"}}
    assert (
        await orchestration_executor.service.repository.list_unprocessed_child_result_messages()
        == []
    )
    task = await orchestration_executor.service.repository.get_task(background.task_id)
    assert task is not None and task.background is False


async def test_hard_terminal_failure_interrupts_queued_same_session_work(tmp_path) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    gate = asyncio.Event()
    started = asyncio.Event()
    run_keys: list[str] = []

    class HardFailureRunner:
        async def run(self, *, activation, session, task, route):
            run_keys.append(task.task_key)
            started.set()
            await gate.wait()
            return ActivationExecutionResult(
                outcome=TaskOutcome.FAILED,
                result={"terminal_reason": "model_repetition_loop_detected"},
            )

        async def interrupt(self, *, activation, reason: str) -> None:
            return None

    async def no_route(task, session) -> Any:
        return None

    orchestration_executor = OrchestrationExecutor(
        service,
        runner=HardFailureRunner(),
        route_child=no_route,
    )
    try:
        first = await orchestration_executor.delegate(_request(task_key="first", background=True))
        await asyncio.wait_for(started.wait(), timeout=1)
        queued = asyncio.create_task(
            orchestration_executor.delegate(
                _request(
                    task_key="queued",
                    background=False,
                    session_id=first.session_id,
                )
            )
        )
        for _ in range(100):
            if await repo.find_task_by_key("run-1", "queued") is not None:
                break
            await asyncio.sleep(0.01)

        gate.set()
        result = await asyncio.wait_for(queued, timeout=1)
        task = await repo.get_task(result.task_id)
        assert run_keys == ["first"]
        assert task is not None and task.outcome is TaskOutcome.INTERRUPTED
        assert result.result == {
            "status": "interrupted",
            "summary": "model_repetition_loop_detected",
            "error": "model_repetition_loop_detected",
            "retry_same_agent": True,
            "reason": "model_repetition_loop_detected",
        }
    finally:
        gate.set()
        await orchestration_executor.close()
        await repo.close()


async def test_durable_append_does_not_depend_on_live_runtime_steering(tmp_path) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    gate = asyncio.Event()

    runner = ControlledRunner(
        gate=gate,
        started=[],
        started_signal=asyncio.Event(),
    )

    async def no_route(task, session) -> Any:
        return None

    orchestration_executor = OrchestrationExecutor(
        service,
        runner=runner,
        route_child=no_route,
    )
    try:
        first = await orchestration_executor.delegate(_request(task_key="first", background=True))
        await asyncio.wait_for(runner.started_signal.wait(), timeout=1)
        appended = asyncio.create_task(
            orchestration_executor.delegate(
                _request(
                    task_key="queued",
                    background=False,
                    session_id=first.session_id,
                )
            )
        )
        await asyncio.sleep(0)
        assert not appended.done()
        gate.set()
        result = await asyncio.wait_for(appended, timeout=1)
        assert result.result == {"summary": "Perform queued", "route": None}
        task = await repo.get_task(result.task_id)
        assert task is not None
        assert task.outcome is TaskOutcome.SUCCEEDED
    finally:
        gate.set()
        await orchestration_executor.close()
        await repo.close()


async def test_background_notifier_failure_does_not_block_queue_promotion(tmp_path) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo, max_direct_children=1)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    gates = [asyncio.Event(), asyncio.Event()]
    starts: list[str] = []
    second_started = asyncio.Event()

    class OrderedRunner(ControlledRunner):
        async def run(self, *, activation, session, task, route) -> ActivationExecutionResult:
            index = len(starts)
            starts.append(task.task_key)
            if index == 1:
                second_started.set()
            await gates[index].wait()
            return ActivationExecutionResult(
                outcome=TaskOutcome.SUCCEEDED,
                result={"summary": task.task_key},
            )

    async def no_route(task, session) -> Any:
        return None

    async def broken_notifier(**kwargs) -> None:
        raise RuntimeError("parent unavailable")

    orchestration_executor = OrchestrationExecutor(
        service,
        runner=OrderedRunner(
            gate=asyncio.Event(),
            started=[],
            started_signal=asyncio.Event(),
        ),
        route_child=no_route,
        notify_parent=broken_notifier,
    )
    try:
        await orchestration_executor.delegate(_request(task_key="first", background=True))
        await orchestration_executor.delegate(_request(task_key="second", background=True))
        while starts != ["first"]:
            await asyncio.sleep(0)
        gates[0].set()
        await asyncio.wait_for(second_started.wait(), timeout=1)
        assert starts == ["first", "second"]
        gates[1].set()
    finally:
        await orchestration_executor.close()
        await repo.close()


async def test_ninth_queued_child_starts_after_a_slot_releases(tmp_path) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo, max_direct_children=1)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    gates = [asyncio.Event(), asyncio.Event()]
    starts: list[str] = []
    started_signals = [asyncio.Event(), asyncio.Event()]

    class OrderedRunner(ControlledRunner):
        async def run(self, *, activation, session, task, route) -> ActivationExecutionResult:
            index = len(starts)
            starts.append(task.task_key)
            started_signals[index].set()
            await gates[index].wait()
            return ActivationExecutionResult(
                outcome=TaskOutcome.SUCCEEDED,
                result={"summary": task.task_key},
            )

    runner = OrderedRunner(
        gate=asyncio.Event(),
        started=[],
        started_signal=asyncio.Event(),
    )

    async def route_child(task, session) -> Any:
        return None

    orchestration_executor = OrchestrationExecutor(
        service,
        runner=runner,
        route_child=route_child,
    )
    try:
        await orchestration_executor.delegate(_request(task_key="first", background=True))
        await orchestration_executor.delegate(_request(task_key="second", background=True))
        await asyncio.wait_for(started_signals[0].wait(), timeout=1)
        assert starts == ["first"]

        gates[0].set()
        await asyncio.wait_for(started_signals[1].wait(), timeout=1)
        assert starts == ["first", "second"]
        gates[1].set()
    finally:
        await orchestration_executor.close()
        await repo.close()


async def test_model_repetition_failure_interrupts_live_descendant_subtree(tmp_path) -> None:
    repo = await OrchestrationRepository.open(
        tmp_path / "orchestration.db",
        clock=lambda: 1_000,
        id_factory=_id_factory(),
    )
    service = OrchestrationService(repo)
    tools = frozenset({"read_file", "delegate_task", "interrupt_agent"})
    await service.start_run(
        run_id="run-1",
        root_session_id="root-session",
        root_task_id="root-task",
        root_task_key="root",
        root_task="Implement",
        mode=OrchestrationMode.COMPLEX,
        worker_template_tools=tools,
        registered_tools=tools,
    )
    parent_may_finish = asyncio.Event()
    parent_started = asyncio.Event()
    child_started = asyncio.Event()
    interrupted: list[str] = []

    class WatchdogRunner(ControlledRunner):
        async def run(self, *, activation, session, task, route) -> ActivationExecutionResult:
            if task.task_key == "parent":
                parent_started.set()
                await parent_may_finish.wait()
                return ActivationExecutionResult(
                    outcome=TaskOutcome.FAILED,
                    result={"terminal_reason": "model_repetition_loop_detected"},
                )
            child_started.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled child resumed")

        async def interrupt(self, *, activation, reason: str) -> None:
            interrupted.append(activation.activation_id)

    async def no_route(task, session) -> Any:
        return None

    runner = WatchdogRunner(
        gate=asyncio.Event(),
        started=[],
        started_signal=asyncio.Event(),
    )
    orchestration_executor = OrchestrationExecutor(
        service,
        runner=runner,
        route_child=no_route,
    )
    try:
        parent = await orchestration_executor.delegate(_request(task_key="parent", background=True))
        await asyncio.wait_for(parent_started.wait(), timeout=1)
        child_call = asyncio.create_task(
            orchestration_executor.delegate(
                DelegateRequest(
                    run_id="run-1",
                    parent_session_id=parent.session_id,
                    parent_task_id=parent.task_id,
                    parent_activation_id=parent.activation_id,
                    task_key="child",
                    task="Perform child",
                    acceptance_criteria="Return the complete child result with evidence",
                    inherited_tools=tools,
                    registered_tools=tools,
                    background=False,
                )
            )
        )
        await asyncio.wait_for(child_started.wait(), timeout=1)
        child_task_record = await repo.find_task_by_key("run-1", "child")
        assert child_task_record is not None
        child_activation_record = await repo.latest_activation_for_task(child_task_record.task_id)
        assert child_activation_record is not None

        parent_may_finish.set()
        for _ in range(100):
            parent_activation = await repo.get_activation(parent.activation_id)
            child_activation = await repo.get_activation(child_activation_record.activation_id)
            if (
                parent_activation is not None
                and parent_activation.phase is ActivationPhase.RELEASED
                and child_activation is not None
                and child_activation.phase is ActivationPhase.RELEASED
            ):
                break
            await asyncio.sleep(0.01)

        parent_activation = await repo.get_activation(parent.activation_id)
        child_activation = await repo.get_activation(child_activation_record.activation_id)
        child_task = await repo.get_task(child_task_record.task_id)
        assert parent_activation is not None
        assert parent_activation.phase is ActivationPhase.RELEASED
        assert child_activation is not None
        assert child_activation.phase is ActivationPhase.RELEASED
        assert child_task is not None
        assert child_task.outcome is TaskOutcome.INTERRUPTED
        assert interrupted == [child_activation_record.activation_id]
        await child_call
    finally:
        await orchestration_executor.close()
        await repo.close()
