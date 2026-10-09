"""Gateway process assembly for the durable orchestration runtime."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from opensquilla.gateway.orchestration_routing import select_child_tree_route
from opensquilla.gateway.orchestration_runtime_runner import (
    TaskRuntimeActivationRunner,
    TaskRuntimeParentNotifier,
)
from opensquilla.memory.embedding import LocalEmbeddingProvider
from opensquilla.orchestration.executor import OrchestrationExecutor
from opensquilla.orchestration.models import OrchestrationMode, RunLifecycle, TaskOutcome
from opensquilla.orchestration.repository import OrchestrationRepository
from opensquilla.orchestration.service import (
    DEFAULT_MAX_DELEGATION_DEPTH,
    DEFAULT_MAX_TASK_ATTEMPTS,
    OrchestrationService,
)
from opensquilla.orchestration.session_recall import SessionRecallEngine

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class OrchestrationRuntime:
    repository: OrchestrationRepository
    service: OrchestrationService
    executor: OrchestrationExecutor
    config: Any
    unbind_executor: Any = None
    _closed: bool = False
    _archive_wake: asyncio.Event | None = None
    _archive_task: asyncio.Task[None] | None = None
    _delivery_wake: asyncio.Event | None = None
    _delivery_task: asyncio.Task[None] | None = None
    _delivery_owner: str = field(default_factory=lambda: f"delivery:{secrets.token_urlsafe(12)}")

    async def start(self) -> None:
        """Reconcile process-owned instances and start logical retention."""

        if self._closed:
            raise RuntimeError("OrchestrationRuntime is closed")
        await self._reconcile_result_deliveries_once()
        await self._reconcile_activations_once()
        await self._deliver_pending_results_once()
        await self._archive_once()
        if self._archive_task is None:
            self._archive_wake = asyncio.Event()
            self._archive_task = asyncio.create_task(
                self._archive_loop(),
                name="orchestration-logical-archive",
            )
        if self._delivery_task is None:
            self._delivery_wake = asyncio.Event()
            self._delivery_task = asyncio.create_task(
                self._delivery_loop(),
                name="orchestration-result-delivery",
            )

    async def _reconcile_activations_once(self) -> None:
        for activation in await self.repository.list_orphaned_activations():
            await self.executor.recover_orphaned_activation(activation)
        await self.executor.recover_startable_activations()

    async def _terminate_failed_root(self, run: Any, *, status: str) -> None:
        reason = f"root task terminated with status {status or 'failed'}"
        targets = await self.repository.list_live_subtree_activations(
            run.root_session_id,
            run_id=run.run_id,
        )
        for target in targets:
            current = await self.repository.get_activation(target.activation_id)
            if current is None or current.phase.value == "released":
                continue
            try:
                await self.executor.interrupt(
                    caller_session_id=run.root_session_id,
                    session_id=current.session_id,
                    activation_id=current.activation_id,
                    reason=reason,
                )
            except Exception:  # noqa: BLE001 - a later target may still converge
                log.warning(
                    "orchestration_root_failure_interrupt_retryable",
                    run_id=run.run_id,
                    activation_id=current.activation_id,
                    exc_info=True,
                )
        root_outcome = TaskOutcome.INTERRUPTED if status == "cancelled" else TaskOutcome.FAILED
        await self.service.terminate_run_without_synthesis(
            run.run_id,
            root_outcome=root_outcome,
            result={
                "reason": f"root_task_{status or 'failed'}",
                "task_status": status or "failed",
            },
        )
        if self._archive_wake is not None:
            self._archive_wake.set()
        await self.executor.publish_progress(
            run_id=run.run_id,
            task_id=run.root_task_id,
            phase="failed",
        )

    async def on_task_lifecycle(self, event: Any) -> None:
        if getattr(event, "phase", None) != "terminal":
            return
        status = getattr(getattr(event, "task_status", None), "value", None)
        has_continuation = bool(getattr(event, "continuation_task_id", None))
        run_id = str(getattr(event, "orchestration_run_id", "") or "")
        run = await self.repository.get_run(run_id) if run_id else None
        if run is None:
            run = await self.repository.get_run_by_root_task_id(str(event.task_id))
        if run is None or run.final_synthesis_completed or run.lifecycle is not RunLifecycle.ACTIVE:
            return
        event_task_id = str(getattr(event, "task_id", "") or "")
        if event_task_id == run.root_task_id and status != "succeeded":
            if not has_continuation:
                await self._terminate_failed_root(run, status=status or "failed")
            return
        result_prefix = "orchestration-result:"
        is_result_event = event_task_id.startswith(result_prefix) and (
            getattr(event, "run_kind", None) == "runtime_send"
        )
        if is_result_event:
            result_identity = event_task_id.removeprefix(result_prefix).split(":delivery:", 1)[0]
            child_task_id, separator, activation_id = result_identity.partition(":activation:")
            activation_id = activation_id if separator else ""
            if status != "succeeded":
                if await self.repository.requeue_child_result_delivery(
                    child_task_id,
                    activation_id=activation_id or None,
                ):
                    if self._delivery_wake is not None:
                        self._delivery_wake.set()
                return
            await self.repository.acknowledge_child_result_message(
                child_task_id,
                activation_id=activation_id or None,
            )
            await self.repository.mark_child_result_processed(
                child_task_id,
                activation_id=activation_id or None,
            )
        elif status != "succeeded" or event_task_id != run.root_task_id:
            return
        if has_continuation and not is_result_event:
            return
        if not await self.repository.run_ready_for_final_synthesis(run.run_id):
            return
        await self.service.mark_final_synthesis_completed(run.run_id)
        await self.executor.publish_progress(
            run_id=run.run_id,
            task_id=run.root_task_id,
            phase="done",
        )

    async def _reconcile_result_deliveries_once(self) -> None:
        notifier = self.executor.notify_parent
        state_reader = getattr(notifier, "delivery_state", None)
        if not callable(state_reader):
            return
        for message in await self.repository.list_unprocessed_child_result_messages():
            task_id = str(message.payload.get("task_id") or "")
            activation_id = str(message.payload.get("activation_id") or "")
            if not task_id or not activation_id:
                continue
            state, _attempt = await state_reader(
                task_id=task_id,
                activation_id=activation_id,
            )
            if state == "active":
                continue
            if state == "succeeded":
                await self.repository.acknowledge_child_result_message(
                    task_id,
                    activation_id=activation_id,
                )
                await self.repository.mark_child_result_processed(
                    task_id,
                    activation_id=activation_id,
                )
                task = await self.repository.get_task(task_id)
                if task is not None and await self.repository.run_ready_for_final_synthesis(
                    task.run_id
                ):
                    await self.service.mark_final_synthesis_completed(task.run_id)
                continue
            await self.repository.requeue_child_result_delivery(
                task_id,
                activation_id=activation_id,
            )

    async def _archive_once(self) -> None:
        subagents = getattr(self.config, "subagents", None)
        retention_minutes = int(getattr(subagents, "archive_after_minutes", 60) or 0)
        if retention_minutes <= 0:
            return
        await self.repository.archive_eligible_runs(
            cutoff=int(time.time() * 1000) - retention_minutes * 60_000
        )

    async def _archive_loop(self) -> None:
        assert self._archive_wake is not None
        while not self._closed:
            try:
                await asyncio.wait_for(self._archive_wake.wait(), timeout=3_600)
            except TimeoutError:
                pass
            if not self._closed:
                try:
                    await self._archive_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - the next archive tick retries
                    log.warning("orchestration_archive_retryable", exc_info=True)

    async def _deliver_pending_results_once(self) -> None:
        notifier = self.executor.notify_parent
        if notifier is None:
            return
        messages = await self.repository.claim_child_result_messages(
            owner=self._delivery_owner,
            lease_expires_at=int(time.time() * 1000) + 30_000,
        )
        for message in messages:
            payload = message.payload
            task_id = str(payload.get("task_id") or "")
            session_id = str(payload.get("session_id") or "")
            activation_id = str(payload.get("activation_id") or "")
            if not task_id or not session_id or not activation_id:
                log.warning(
                    "orchestration_result_delivery_invalid",
                    message_id=message.message_id,
                )
                continue
            task = await self.repository.get_task(task_id)
            session = await self.repository.get_session(session_id)
            if task is None or session is None:
                log.warning(
                    "orchestration_result_delivery_orphaned",
                    message_id=message.message_id,
                    task_id=task_id,
                    session_id=session_id,
                )
                continue
            try:
                outcome = TaskOutcome(str(payload.get("outcome") or task.outcome.value))
                result = payload.get("result")
                completed = await notifier(
                    session=session,
                    task=task,
                    activation_id=activation_id,
                    outcome=outcome,
                    result=dict(result) if isinstance(result, dict) else {},
                )
                if completed:
                    await self.service.acknowledge_result_delivery(
                        task.task_id,
                        activation_id=activation_id,
                        owner=self._delivery_owner,
                    )
                    await self.repository.mark_child_result_processed(
                        task.task_id,
                        activation_id=activation_id,
                    )
            except Exception as exc:  # noqa: BLE001 - retry remains durable
                await self.repository.release_child_result_claim(
                    task.task_id,
                    activation_id=activation_id,
                    owner=self._delivery_owner,
                )
                log.warning(
                    "orchestration_result_delivery_retryable",
                    message_id=message.message_id,
                    task_id=task_id,
                    error=str(exc),
                )

    async def _delivery_loop(self) -> None:
        assert self._delivery_wake is not None
        while not self._closed:
            try:
                await asyncio.wait_for(self._delivery_wake.wait(), timeout=5)
            except TimeoutError:
                pass
            if not self._closed:
                self._delivery_wake.clear()
                try:
                    await self._deliver_pending_results_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - the next delivery tick retries
                    log.warning("orchestration_result_delivery_loop_retryable", exc_info=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._archive_wake is not None:
            self._archive_wake.set()
        if self._archive_task is not None:
            self._archive_task.cancel()
            await asyncio.gather(self._archive_task, return_exceptions=True)
            self._archive_task = None
        if self._delivery_wake is not None:
            self._delivery_wake.set()
        if self._delivery_task is not None:
            self._delivery_task.cancel()
            await asyncio.gather(self._delivery_task, return_exceptions=True)
            self._delivery_task = None
        if callable(self.unbind_executor):
            self.unbind_executor()
        await self.executor.close()
        from opensquilla.gateway.rpc_delegated_results import unbind_delegated_result_repository

        unbind_delegated_result_repository(self.repository)
        await self.repository.close()


def build_orchestration_runtime(
    *,
    repository: OrchestrationRepository,
    session_manager: Any,
    task_runtime: Any,
    config: Any,
    registry: Any,
    bind_executor: Any = None,
    event_emitter: Callable[[str, str, dict[str, Any]], Awaitable[None]] | None = None,
) -> OrchestrationRuntime:
    from opensquilla.gateway.rpc_delegated_results import bind_delegated_result_repository

    bind_delegated_result_repository(repository)
    subagents = getattr(config, "subagents", None)
    max_delegation_depth = int(
        getattr(subagents, "max_spawn_depth", DEFAULT_MAX_DELEGATION_DEPTH)
        or DEFAULT_MAX_DELEGATION_DEPTH
    )
    max_task_attempts = int(
        getattr(subagents, "max_task_attempts", DEFAULT_MAX_TASK_ATTEMPTS)
        or DEFAULT_MAX_TASK_ATTEMPTS
    )
    session_recall = SessionRecallEngine(LocalEmbeddingProvider())
    service = OrchestrationService(
        repository,
        max_delegation_depth=max_delegation_depth,
        max_task_attempts=max_task_attempts,
        session_recall=session_recall,
    )
    runner = TaskRuntimeActivationRunner(
        repository=repository,
        session_manager=session_manager,
        task_runtime=task_runtime,
        config=config,
        session_recall=session_recall,
    )

    async def publish_progress(run_id: str, task_id: str, phase: str) -> None:
        if event_emitter is None:
            return
        run = await repository.get_run(run_id)
        if run is None or run.mode is not OrchestrationMode.COMPLEX:
            return
        root = await repository.get_session(run.root_session_id)
        task = await repository.get_task(task_id)
        if root is None or not root.runtime_session_key or task is None:
            return
        root_task = await repository.get_task(run.root_task_id)
        single_mode = bool(
            root_task is not None and root_task.runtime_context.get("single_agent_mode")
        )
        status = {
            "waiting": "assigned",
            "working": "working",
            "add": "planned",
            "start": "working",
            "complete": "completed",
            "block": "blocked",
            "reopen": "planned",
            "completed": "completed",
            "failed": "failed",
            "done": "finished",
        }.get(phase, phase)
        summary = str((task.result or {}).get("summary") or "").strip()
        if not summary and phase in {"complete", "block"} and task.evidence:
            summary = task.evidence[-1]
        next_step = {
            "waiting": "Wait for this child",
            "working": "Continue this step",
            "add": "Start this step",
            "start": "Continue this step",
            "complete": "Continue the planned work",
            "block": "Resolve the reported blocker",
            "reopen": "Restart this step",
            "completed": "Continue the planned work",
            "failed": "Handle this failed step",
            "done": "None",
        }.get(phase, "Continue the planned work")
        message = f"{task.task_key}: {status}."
        if summary:
            message += f" {summary}"
        message += f" Next: {next_step}."
        event_name = (
            "session.event.task_group.done"
            if phase == "done"
            else "session.event.task_group.failed"
            if phase == "failed" and task_id == run.root_task_id
            else "session.event.task_group.waiting"
        )
        payload: dict[str, Any] = {
            "key": root.runtime_session_key,
            "task_id": run.root_task_id,
            "group_id": run.run_id,
            "message": message,
        }
        if single_mode and task_id != run.root_task_id and phase in {"completed", "failed"}:
            payload["result_task_id"] = task_id
        await event_emitter(
            root.runtime_session_key,
            event_name,
            payload,
        )

    async def route_child(task: Any, session: Any) -> Any:
        context = dict(task.runtime_context)
        baseline_model = str(context.get("active_model") or "").strip()
        if not baseline_model:
            baseline_model = str(getattr(getattr(config, "llm", None), "model", "") or "").strip()
        routing_task = task.description
        if context.get("single_agent_mode") and task.parent_task_id:
            parent = await repository.get_task(task.parent_task_id)
            if parent is not None and parent.parent_task_id is None:
                routing_task = parent.description
        return await select_child_tree_route(
            routing_task,
            session_key=session.runtime_session_key or session.session_id,
            baseline_model=baseline_model,
            config=config,
        )

    executor = OrchestrationExecutor(
        service,
        runner=runner,
        route_child=route_child,
        progress_notifier=publish_progress,
        foreground_wait_scope=getattr(task_runtime, "foreground_child_wait", None),
        notify_parent=TaskRuntimeParentNotifier(
            repository=repository,
            task_runtime=task_runtime,
            serialization_lock=service.run_serialization_lock,
            retry_same_agent_check=service.can_retry_same_agent,
            agent_state_check=service.agent_state,
        ),
    )
    if callable(bind_executor):
        bind_executor(executor, registry=registry)
    return OrchestrationRuntime(
        repository=repository,
        service=service,
        executor=executor,
        config=config,
        unbind_executor=((lambda: bind_executor(None)) if callable(bind_executor) else None),
    )


__all__ = ["OrchestrationRuntime", "build_orchestration_runtime"]
