"""Foreground/background execution adapter for durable orchestration records."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any, Protocol

import structlog

from opensquilla.orchestration.models import (
    ActivationPhase,
    AgentActivationRecord,
    AgentSessionRecord,
    AgentState,
    DelegatedTaskRecord,
    OrchestrationMode,
    TaskOutcome,
)
from opensquilla.orchestration.service import (
    DelegateDisposition,
    DelegateOutcome,
    DelegateRequest,
    InterruptFenceError,
    OrchestrationService,
)
from opensquilla.orchestration.watchdog import terminal_subtree_interrupt_reason


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivationExecutionResult:
    outcome: TaskOutcome
    result: dict[str, Any]


def _structured_failure_result(
    outcome: TaskOutcome,
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    """Normalize runtime failures without discarding diagnostic details."""

    payload = dict(result or {})
    summary = str(
        payload.get("summary") or payload.get("error") or payload.get("reason") or outcome.value
    ).strip()
    error = str(payload.get("error") or payload.get("reason") or summary).strip()
    requested_retry = payload.get("retry_same_agent")
    payload.update(
        {
            "status": ("interrupted" if outcome is TaskOutcome.INTERRUPTED else "failed"),
            "summary": summary,
            "error": error,
            # Infrastructure failures are recoverable by default. The public
            # tool and background notifier still fence this against the
            # durable attempt budget before telling the parent to retry.
            "retry_same_agent": (requested_retry if isinstance(requested_retry, bool) else True),
        }
    )
    return payload


class ActivationRunner(Protocol):
    async def run(
        self,
        *,
        activation: AgentActivationRecord,
        session: AgentSessionRecord,
        task: DelegatedTaskRecord,
        route: Any,
    ) -> ActivationExecutionResult: ...

    async def interrupt(
        self,
        *,
        activation: AgentActivationRecord,
        reason: str,
    ) -> None: ...


RouteChild = Callable[
    [DelegatedTaskRecord, AgentSessionRecord],
    Awaitable[Any],
]
ParentNotifier = Callable[..., Awaitable[bool]]
ProgressNotifier = Callable[[str, str, str], Awaitable[None]]
ForegroundWaitScope = Callable[[], AbstractAsyncContextManager[None]]
log = structlog.get_logger(__name__)


@asynccontextmanager
async def _noop_foreground_wait_scope() -> AsyncIterator[None]:
    yield


@dataclass(frozen=True, slots=True, kw_only=True)
class DelegateExecution:
    disposition: DelegateDisposition
    run_id: str
    task_id: str
    task_key: str
    session_id: str
    activation_id: str
    phase: ActivationPhase
    background: bool
    result: dict[str, Any] | None = None


class OrchestrationExecutor:
    """Own live drivers while the service owns all persistent lifecycle state.

    Foreground callers await a task future here.  Background callers return
    immediately; terminal delivery is persisted by ``OrchestrationService``.
    A queued activation does not receive a driver until the service promotes
    it after another direct child releases its slot.
    """

    def __init__(
        self,
        service: OrchestrationService,
        *,
        runner: ActivationRunner,
        route_child: RouteChild,
        notify_parent: ParentNotifier | None = None,
        progress_notifier: ProgressNotifier | None = None,
        foreground_wait_scope: ForegroundWaitScope | None = None,
    ) -> None:
        self.service = service
        self.runner = runner
        self.route_child = route_child
        self.notify_parent = notify_parent
        self.progress_notifier = progress_notifier
        self._foreground_wait_scope = foreground_wait_scope or _noop_foreground_wait_scope
        self._drivers: dict[str, asyncio.Task[None]] = {}
        self._waiters: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = {}
        self._delivery_owner = f"executor:{secrets.token_urlsafe(12)}"
        self._interrupt_timeout_seconds = 2.0
        self._closed = False

    async def delegate(self, request: DelegateRequest) -> DelegateExecution:
        if self._closed:
            raise RuntimeError("OrchestrationExecutor is closed")
        outcome = await self.service.delegate(request)

        if outcome.disposition is DelegateDisposition.REUSED:
            if not request.background and outcome.task.background:
                await self.service.observe_child_result(
                    outcome.task.task_id,
                    activation_id=outcome.activation.activation_id,
                )
                await self._cancel_pending_result_delivery(
                    task=outcome.task,
                    session=outcome.session,
                    activation_id=outcome.activation.activation_id,
                )
            return self._execution_from_outcome(
                outcome,
                background=request.background,
                result=outcome.result,
            )

        await self.publish_progress(
            run_id=outcome.task.run_id,
            task_id=outcome.task.task_id,
            phase="waiting",
        )
        try:
            return await self._complete_persisted_delegation(request, outcome)
        except asyncio.CancelledError as exc:
            if not getattr(exc, "_orchestration_cleanup_complete", False):
                await self._interrupt_cancelled_delegation(request, outcome)
            raise

    async def publish_progress(self, *, run_id: str, task_id: str, phase: str) -> None:
        """Project child progress without making UI delivery part of task success."""

        if self.progress_notifier is None:
            return
        try:
            await self.progress_notifier(run_id, task_id, phase)
        except Exception:  # noqa: BLE001 - UI notification is best effort
            log.warning(
                "orchestration_progress_notification_failed",
                run_id=run_id,
                task_id=task_id,
                phase=phase,
                exc_info=True,
            )

    async def can_retry_same_agent(self, *, task_id: str, session_id: str) -> bool:
        return await self.service.can_retry_same_agent(
            task_id=task_id,
            session_id=session_id,
        )

    async def _complete_persisted_delegation(
        self,
        request: DelegateRequest,
        outcome: DelegateOutcome,
    ) -> DelegateExecution:
        if (
            outcome.disposition in {DelegateDisposition.RETRIED, DelegateDisposition.REPLACED}
            and outcome.task.retry_of_activation_id
        ):
            await self._cancel_pending_result_delivery(
                task=outcome.task,
                session=outcome.session,
                activation_id=outcome.task.retry_of_activation_id,
            )

        task_owns_activation = outcome.activation.task_id == outcome.task.task_id
        waiter = (
            self._activation_waiter(outcome.activation.activation_id)
            if task_owns_activation
            else self._queued_waiter(outcome.task.task_id)
        )
        if task_owns_activation and outcome.activation.phase is ActivationPhase.STARTING:
            self._ensure_driver(outcome.activation.activation_id)
        if request.background:
            return self._execution_from_outcome(outcome, background=True)

        parent_waiting = await self._mark_parent_waiting(
            request=request,
            child_activation_id=outcome.activation.activation_id,
        )
        try:
            async with self._foreground_wait_scope():
                try:
                    result = await asyncio.shield(waiter)
                except asyncio.CancelledError as exc:
                    # The wait scope reacquires the parent's TaskRuntime slot on
                    # exit. Interrupt and settle the child first, otherwise a
                    # single-slot runtime can wait forever for the child-owned
                    # slot while the cancellation handler waits outside.
                    await self._interrupt_cancelled_delegation(request, outcome)
                    setattr(exc, "_orchestration_cleanup_complete", True)
                    raise
        finally:
            if parent_waiting:
                await self._restore_parent_working(request)
        terminal_task = await self.service.repository.get_task(outcome.task.task_id)
        if terminal_task is None:
            raise RuntimeError("delegated task disappeared after completion")
        if terminal_task.background:
            completed_activation = await self.service.repository.latest_activation_for_task(
                terminal_task.task_id
            )
            observed_activation_id = (
                completed_activation.activation_id
                if completed_activation is not None
                else outcome.activation.activation_id
            )
            await self.service.observe_child_result(
                terminal_task.task_id,
                activation_id=observed_activation_id,
            )
            await self._cancel_pending_result_delivery(
                task=terminal_task,
                session=outcome.session,
                activation_id=observed_activation_id,
            )
        return self._execution_from_outcome(
            outcome,
            background=False,
            result=result,
            phase=ActivationPhase.RELEASED,
        )

    async def _interrupt_cancelled_delegation(
        self,
        request: DelegateRequest,
        outcome: DelegateOutcome,
    ) -> None:
        cleanup = asyncio.create_task(
            self.interrupt(
                caller_session_id=request.parent_session_id,
                session_id=outcome.session.session_id,
                activation_id=outcome.activation.activation_id,
                reason="delegation cancelled before settlement",
            )
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
        except InterruptFenceError:
            current_task = await self.service.repository.get_task(outcome.task.task_id)
            if current_task is not None and current_task.outcome is TaskOutcome.PENDING:
                raise

    async def _mark_parent_waiting(
        self,
        *,
        request: DelegateRequest,
        child_activation_id: str,
    ) -> bool:
        if request.parent_activation_id is None:
            return False
        current = await self.service.repository.refresh_parent_foreground_wait(
            run_id=request.run_id,
            parent_session_id=request.parent_session_id,
            parent_task_id=request.parent_task_id,
            parent_activation_id=request.parent_activation_id,
            preferred_child_activation_id=child_activation_id,
        )
        return current is not None

    async def _restore_parent_working(self, request: DelegateRequest) -> None:
        assert request.parent_activation_id is not None
        await self.service.repository.refresh_parent_foreground_wait(
            run_id=request.run_id,
            parent_session_id=request.parent_session_id,
            parent_task_id=request.parent_task_id,
            parent_activation_id=request.parent_activation_id,
        )

    async def ensure_run(
        self,
        *,
        run_id: str,
        root_session_id: str,
        root_task_id: str,
        mode: OrchestrationMode,
        worker_template_tools: frozenset[str],
        registered_tools: frozenset[str],
        root_runtime_session_key: str | None,
        root_runtime_context: dict[str, Any] | None = None,
        root_task: str | None = None,
    ) -> None:
        await self.service.ensure_run(
            run_id=run_id,
            root_session_id=root_session_id,
            root_task_id=root_task_id,
            root_task_key="root",
            root_task=(root_task or "Root agent task"),
            mode=mode,
            worker_template_tools=worker_template_tools,
            registered_tools=registered_tools,
            root_runtime_session_key=root_runtime_session_key,
            root_runtime_context=root_runtime_context,
        )

    async def agent_state(self, *, run_id: str, session_id: str) -> AgentState:
        return await self.service.agent_state(run_id=run_id, session_id=session_id)

    async def interrupt(
        self,
        *,
        caller_session_id: str,
        session_id: str,
        activation_id: str,
        reason: str,
    ) -> list[AgentActivationRecord]:
        targets = await self.service.request_interrupt(
            caller_session_id=caller_session_id,
            session_id=session_id,
            activation_id=activation_id,
            reason=reason,
        )
        for target in targets:
            try:
                await asyncio.wait_for(
                    self.runner.interrupt(activation=target, reason=reason),
                    timeout=self._interrupt_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - interruption must fail closed
                log.warning(
                    "orchestration_activation_interrupt_failed",
                    activation_id=target.activation_id,
                    reason=reason,
                    error=str(exc),
                )
            driver = self._drivers.get(target.activation_id)
            if driver is not None and not driver.done():
                driver.cancel()
                await asyncio.gather(driver, return_exceptions=True)
                task = await self.service.repository.get_task(target.task_id)
                if task is not None:
                    await self._settle_cancelled(target, task)
                continue
            task = await self.service.repository.get_task(target.task_id)
            if task is not None and task.outcome is TaskOutcome.PENDING:
                result = _structured_failure_result(
                    TaskOutcome.INTERRUPTED,
                    {"reason": reason},
                )
                settlement = await self.service.settle_activation(
                    target.activation_id,
                    outcome=TaskOutcome.INTERRUPTED,
                    result=result,
                    continue_session=False,
                )
                self._resolve_activation_waiter(
                    target.activation_id,
                    result,
                )
                self._resolve_queued_waiter(task.task_id, result)
                for interrupted_task_id in settlement.interrupted_task_ids:
                    self._resolve_queued_waiter(
                        interrupted_task_id,
                        result,
                    )
        return targets

    async def recover_orphaned_activation(
        self,
        activation: AgentActivationRecord,
    ) -> None:
        """Release one activation left live by a previous Gateway process."""

        session = await self.service.repository.get_session(activation.session_id)
        task = await self.service.repository.get_task(activation.task_id)
        if session is None or task is None:
            raise RuntimeError("orphaned activation is missing its durable session or task")
        result = _structured_failure_result(
            TaskOutcome.INTERRUPTED,
            {"reason": "gateway_restarted", "recoverable": True},
        )
        settlement = await self.service.settle_activation(
            activation.activation_id,
            outcome=TaskOutcome.INTERRUPTED,
            result=result,
            continue_session=activation.phase is not ActivationPhase.STOPPING,
        )
        try:
            await asyncio.wait_for(
                self.runner.interrupt(
                    activation=activation,
                    reason="gateway_restarted",
                ),
                timeout=self._interrupt_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - durable settlement already won
            log.warning(
                "orchestration_orphaned_activation_interrupt_failed",
                activation_id=activation.activation_id,
                error=str(exc),
            )
        driver = self._drivers.get(activation.activation_id)
        if driver is not None and not driver.done():
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
        await self._notify_if_background_safely(
            activation=activation,
            session=session,
            task=task,
            outcome=TaskOutcome.INTERRUPTED,
            result=result,
        )
        self._resolve_activation_waiter(activation.activation_id, result)
        self._resolve_queued_waiter(task.task_id, result)
        for interrupted_task_id in settlement.interrupted_task_ids:
            self._resolve_queued_waiter(interrupted_task_id, result)
        if settlement.promoted is not None and not self._closed:
            self._ensure_driver(settlement.promoted.activation_id)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        drivers = list(self._drivers.values())
        for driver in drivers:
            if not driver.done():
                driver.cancel()
        if drivers:
            await asyncio.gather(*drivers, return_exceptions=True)

    def _waiter(self, kind: str, identity: str) -> asyncio.Future[dict[str, Any]]:
        key = (kind, identity)
        waiter = self._waiters.get(key)
        if waiter is None:
            waiter = asyncio.get_running_loop().create_future()
            self._waiters[key] = waiter
        return waiter

    def _activation_waiter(
        self,
        activation_id: str,
    ) -> asyncio.Future[dict[str, Any]]:
        return self._waiter("activation", activation_id)

    def _queued_waiter(self, task_id: str) -> asyncio.Future[dict[str, Any]]:
        return self._waiter("queued", task_id)

    def _resolve_waiter(
        self,
        kind: str,
        identity: str,
        result: dict[str, Any],
    ) -> None:
        key = (kind, identity)
        waiter = self._waiters.pop(key, None)
        if waiter is None:
            return
        if not waiter.done():
            waiter.set_result(result)

    def _resolve_activation_waiter(
        self,
        activation_id: str,
        result: dict[str, Any],
    ) -> None:
        self._resolve_waiter("activation", activation_id, result)

    def _resolve_queued_waiter(self, task_id: str, result: dict[str, Any]) -> None:
        self._resolve_waiter("queued", task_id, result)

    def _ensure_driver(self, activation_id: str) -> None:
        existing = self._drivers.get(activation_id)
        if existing is not None and not existing.done():
            return
        driver = asyncio.create_task(
            self._drive(activation_id),
            name=f"orchestration-activation:{activation_id}",
        )
        self._drivers[activation_id] = driver

        def discard(completed: asyncio.Task[None]) -> None:
            if self._drivers.get(activation_id) is completed:
                self._drivers.pop(activation_id, None)

        driver.add_done_callback(discard)

    async def _drive(self, activation_id: str) -> None:
        activation = await self.service.repository.claim_activation(activation_id)
        if activation is None:
            return
        session = await self.service.repository.get_session(activation.session_id)
        task = await self.service.repository.get_task(activation.task_id)
        if session is None or task is None:
            raise RuntimeError("activation is missing its durable session or task")

        await self.publish_progress(
            run_id=task.run_id,
            task_id=task.task_id,
            phase="working",
        )

        try:
            try:
                route: Any = None
                if activation.route_required:
                    route = await self.route_child(task, session)
                execution = await self.runner.run(
                    activation=activation,
                    session=session,
                    task=task,
                    route=route,
                )
            except Exception as exc:  # noqa: BLE001 - child failure is a task outcome
                execution = ActivationExecutionResult(
                    outcome=TaskOutcome.FAILED,
                    result={"error": str(exc), "error_type": type(exc).__name__},
                )

            if execution.outcome is not TaskOutcome.SUCCEEDED:
                execution = replace(
                    execution,
                    result=_structured_failure_result(
                        execution.outcome,
                        execution.result,
                    ),
                )

            interrupt_reason = terminal_subtree_interrupt_reason(
                execution.outcome,
                execution.result,
            )
            if interrupt_reason is not None:
                await self._interrupt_unhealthy_subtree(
                    activation=activation,
                    session=session,
                    reason=interrupt_reason,
                )

            settlement = await self.service.settle_activation(
                activation_id,
                outcome=execution.outcome,
                result=execution.result,
                continue_session=interrupt_reason is None,
            )
            await self.publish_progress(
                run_id=task.run_id,
                task_id=task.task_id,
                phase=("completed" if settlement.outcome is TaskOutcome.SUCCEEDED else "failed"),
            )
            await self._notify_if_background_safely(
                activation=activation,
                session=session,
                task=task,
                outcome=settlement.outcome,
                result=settlement.result,
            )
            self._resolve_activation_waiter(activation_id, settlement.result)
            self._resolve_queued_waiter(task.task_id, settlement.result)
            interrupted_result = _structured_failure_result(
                TaskOutcome.INTERRUPTED,
                {"reason": interrupt_reason or "ancestor activation interrupted"},
            )
            for interrupted_task_id in settlement.interrupted_task_ids:
                self._resolve_queued_waiter(interrupted_task_id, interrupted_result)
            if settlement.promoted is not None:
                self._ensure_driver(settlement.promoted.activation_id)
        except asyncio.CancelledError:
            await asyncio.shield(self._settle_cancelled(activation, task))
            raise

    async def recover_startable_activations(self) -> None:
        for activation in await self.service.repository.list_startable_activations():
            self._ensure_driver(activation.activation_id)

    async def _interrupt_unhealthy_subtree(
        self,
        *,
        activation: AgentActivationRecord,
        session: AgentSessionRecord,
        reason: str,
    ) -> None:
        targets = await self.service.request_interrupt(
            caller_session_id=session.session_id,
            session_id=session.session_id,
            activation_id=activation.activation_id,
            reason=reason,
        )
        cancelled_drivers: list[asyncio.Task[None]] = []
        for target in targets:
            if target.activation_id == activation.activation_id:
                continue
            try:
                await asyncio.wait_for(
                    self.runner.interrupt(activation=target, reason=reason),
                    timeout=self._interrupt_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - cancellation must fail closed
                log.warning(
                    "orchestration_descendant_interrupt_failed",
                    activation_id=target.activation_id,
                    reason=reason,
                    error=str(exc),
                )
            driver = self._drivers.get(target.activation_id)
            if driver is not None and not driver.done():
                driver.cancel()
                cancelled_drivers.append(driver)
                continue
            current_task = await self.service.repository.get_task(target.task_id)
            if current_task is not None and current_task.outcome is TaskOutcome.PENDING:
                result = _structured_failure_result(
                    TaskOutcome.INTERRUPTED,
                    {"reason": reason},
                )
                settlement = await self.service.settle_activation(
                    target.activation_id,
                    outcome=TaskOutcome.INTERRUPTED,
                    result=result,
                    continue_session=False,
                )
                self._resolve_activation_waiter(
                    target.activation_id,
                    result,
                )
                self._resolve_queued_waiter(
                    current_task.task_id,
                    result,
                )
                for interrupted_task_id in settlement.interrupted_task_ids:
                    self._resolve_queued_waiter(
                        interrupted_task_id,
                        result,
                    )
        if cancelled_drivers:
            await asyncio.gather(*cancelled_drivers, return_exceptions=True)

    async def _notify_if_background_safely(
        self,
        *,
        activation: AgentActivationRecord,
        session: AgentSessionRecord,
        task: DelegatedTaskRecord,
        outcome: TaskOutcome,
        result: dict[str, Any],
    ) -> None:
        try:
            await self._notify_if_background(
                activation=activation,
                session=session,
                task=task,
                outcome=outcome,
                result=result,
            )
        except Exception as exc:  # noqa: BLE001 - durable inbox is the fallback
            log.warning(
                "orchestration_parent_notification_failed",
                task_id=task.task_id,
                session_id=session.session_id,
                error=str(exc),
            )

    async def _cancel_pending_result_delivery(
        self,
        *,
        task: DelegatedTaskRecord,
        session: AgentSessionRecord,
        activation_id: str,
    ) -> None:
        cancel_delivery = getattr(self.notify_parent, "cancel_pending_delivery", None)
        if not callable(cancel_delivery):
            return
        try:
            await cancel_delivery(
                session=session,
                task=task,
                activation_id=activation_id,
            )
        except Exception as exc:  # noqa: BLE001 - result is already observed durably
            log.warning(
                "orchestration_parent_notification_cancel_failed",
                task_id=task.task_id,
                activation_id=activation_id,
                error=str(exc),
            )

    async def _notify_if_background(
        self,
        *,
        activation: AgentActivationRecord,
        session: AgentSessionRecord,
        task: DelegatedTaskRecord,
        outcome: TaskOutcome,
        result: dict[str, Any],
    ) -> None:
        if not task.background:
            return
        if self.notify_parent is None:
            return
        claimed = await self.service.repository.claim_child_result_messages(
            owner=self._delivery_owner,
            lease_expires_at=int(time.time() * 1000) + 30_000,
            limit=1,
            task_id=task.task_id,
            activation_id=activation.activation_id,
        )
        if not claimed:
            return
        try:
            completed = await self.notify_parent(
                session=session,
                task=task,
                activation_id=activation.activation_id,
                outcome=outcome,
                result=result,
            )
            if completed:
                await self.service.acknowledge_result_delivery(
                    task.task_id,
                    activation_id=activation.activation_id,
                    owner=self._delivery_owner,
                )
                await self.service.repository.mark_child_result_processed(
                    task.task_id,
                    activation_id=activation.activation_id,
                )
        except BaseException:
            await self.service.repository.release_child_result_claim(
                task.task_id,
                activation_id=activation.activation_id,
                owner=self._delivery_owner,
            )
            raise

    async def _settle_cancelled(
        self,
        activation: AgentActivationRecord,
        task: DelegatedTaskRecord,
    ) -> None:
        current_task = await self.service.repository.get_task(task.task_id)
        if current_task is None:
            return
        result = (
            dict(current_task.result)
            if current_task.result is not None
            else {"reason": "activation interrupted"}
        )
        session = await self.service.repository.get_session(activation.session_id)
        if current_task.outcome is TaskOutcome.PENDING:
            result = _structured_failure_result(TaskOutcome.INTERRUPTED, result)
            settlement = await self.service.settle_activation(
                activation.activation_id,
                outcome=TaskOutcome.INTERRUPTED,
                result=result,
                continue_session=False,
            )
            if session is not None:
                await self._notify_if_background_safely(
                    activation=activation,
                    session=session,
                    task=task,
                    outcome=TaskOutcome.INTERRUPTED,
                    result=result,
                )
            for interrupted_task_id in settlement.interrupted_task_ids:
                self._resolve_queued_waiter(interrupted_task_id, result)
            if settlement.promoted is not None and not self._closed:
                self._ensure_driver(settlement.promoted.activation_id)
        self._resolve_activation_waiter(activation.activation_id, result)
        self._resolve_queued_waiter(task.task_id, result)
        await self._resolve_terminal_queued_waiters(activation.session_id)

    async def _resolve_terminal_queued_waiters(self, session_id: str) -> None:
        for kind, task_id in tuple(self._waiters):
            if kind != "queued":
                continue
            task = await self.service.repository.get_task(task_id)
            if (
                task is None
                or task.owner_session_id != session_id
                or task.outcome is TaskOutcome.PENDING
            ):
                continue
            self._resolve_queued_waiter(task_id, dict(task.result or {}))

    @staticmethod
    def _execution_from_outcome(
        outcome: DelegateOutcome,
        *,
        background: bool,
        result: dict[str, Any] | None = None,
        phase: ActivationPhase | None = None,
    ) -> DelegateExecution:
        return DelegateExecution(
            disposition=outcome.disposition,
            run_id=outcome.task.run_id,
            task_id=outcome.task.task_id,
            task_key=outcome.task.task_key,
            session_id=outcome.session.session_id,
            activation_id=outcome.activation.activation_id,
            phase=phase or outcome.activation.phase,
            background=background,
            result=result,
        )


__all__ = [
    "ActivationExecutionResult",
    "ActivationRunner",
    "DelegateExecution",
    "OrchestrationExecutor",
    "ParentNotifier",
    "RouteChild",
]
