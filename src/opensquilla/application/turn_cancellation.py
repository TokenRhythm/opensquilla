"""Cancellation ownership and deadlines, independent of RPC and wire events."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import structlog

from opensquilla.application.turn_admission import CancelTurn, CancelTurnResult

log = structlog.get_logger(__name__)


class ExactCancellationUnavailableError(RuntimeError):
    """The runtime cannot prove both task and session ownership atomically."""


@dataclass(frozen=True, slots=True)
class CancellationTiming:
    response_seconds: float = 2.0
    cleanup_seconds: float = 30.0
    lookup_seconds: float = 0.05
    tree_passes: int = 8


class CancellationPrimitives(Protocol):
    @property
    def session_available(self) -> bool: ...

    @property
    def runtime_available(self) -> bool: ...

    async def session_exists(self, key: str) -> bool: ...

    def cancel_compactions(self, key: str) -> tuple[asyncio.Task[object], ...]: ...

    async def cancel_runtime(self, key: str, task_id: str | None, source: str) -> int: ...

    async def cancel_auxiliary(self, key: str, task_id: str, deadline: float) -> int: ...

    async def cancel_descendants(
        self,
        key: str,
        task_id: str,
        source: str,
        deadline: float,
    ) -> int: ...

    async def active_task_ids(self, key: str) -> tuple[str, ...]: ...

    async def session_tree(self, key: str) -> tuple[str, ...]: ...

    async def cancel_completion(self, key: str) -> int: ...

    async def cancel_processes(self, key: str) -> int: ...

    def reject_approvals(self, key: str) -> int: ...

    async def drain(self, key: str, task_ids: tuple[str, ...], deadline: float) -> None: ...

    def cancel_legacy(self, key: str) -> tuple[bool, bool]: ...

    async def publish_terminal(self, key: str, *, legacy: bool) -> None: ...

    async def bounded[T](
        self,
        operation: Awaitable[T],
        deadline: float,
        label: str,
        default: T,
    ) -> T: ...

    async def observe[T](
        self,
        task: asyncio.Task[T],
        deadline: float,
        label: str,
        default: T,
    ) -> T: ...


def _consume_cleanup[T](task: asyncio.Future[T]) -> None:
    with contextlib.suppress(BaseException):
        task.result()


class TurnCancellation:
    """Own exact-task cleanup and bounded session-tree cancellation ordering."""

    def __init__(
        self,
        ports: CancellationPrimitives,
        *,
        timing: CancellationTiming,
        clock: Callable[[], float],
    ) -> None:
        self._ports = ports
        self._timing = timing
        self._clock = clock

    async def cancel(self, command: CancelTurn) -> CancelTurnResult:
        ports, timing = self._ports, self._timing
        key, task_id = command.session_key, command.task_id
        if not ports.session_available:
            return {"aborted": False, "key": key}
        if command.task_scoped and task_id is None:
            return {"aborted": False, "key": key, "reason": "task_id_required"}
        default_source = "webui_abort" if command.surface == "webchat" else "sessions_abort"
        text = (command.source or "").strip()
        source = "".join(ch if ch.isalnum() or ch in "_-.:" else "_" for ch in text)
        source = (source.strip("_") or default_source)[:80]
        deadline = self._clock() + timing.response_seconds
        compactions = ports.cancel_compactions(key) if task_id is None else ()
        exact: tuple[asyncio.Task[int | None], asyncio.Task[int], asyncio.Task[int]] | None = None
        if task_id is not None and ports.runtime_available:
            # Safety work starts before storage can consume the response budget.
            # Its lifetime is independent from observing the RPC response.
            cleanup_deadline = self._clock() + timing.cleanup_seconds
            runtime_cleanup: asyncio.Task[int | None] = asyncio.create_task(
                ports.bounded(
                    ports.cancel_runtime(key, task_id, source),
                    cleanup_deadline,
                    "cancel_requested_runtime_task_cleanup",
                    None,
                )
            )
            exact = (
                runtime_cleanup,
                asyncio.create_task(ports.cancel_auxiliary(key, task_id, cleanup_deadline)),
                asyncio.create_task(
                    ports.cancel_descendants(key, task_id, source, cleanup_deadline)
                ),
            )
            for task in exact:
                task.add_done_callback(_consume_cleanup)
        exists = await ports.bounded(
            ports.session_exists(key),
            min(deadline, self._clock() + timing.lookup_seconds),
            "session_lookup",
            None,
        )
        if exists is False:
            raise KeyError(f"Session not found: {key}")
        if exists is None:
            log.warning("sessions.abort.session_lookup_deferred", session_key=key)
        if compactions:
            done, _pending = await asyncio.wait(
                compactions,
                timeout=max(0.0, deadline - self._clock()),
            )
            for compaction_task in done:
                try:
                    compaction_task.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    log.warning("sessions.abort.compaction_drain_failed", session_key=key)
        if ports.runtime_available:
            if task_id is not None:
                assert exact is not None
                return await self._exact(key, task_id, exact, deadline)
            return await self._tree(key, source, len(compactions), deadline)
        # Session-only legacy registries cannot establish exact task ownership.
        if task_id is not None or command.task_scoped:
            return {"aborted": False, "key": key, "reason": "task_scope_unsupported"}
        cancelled, needs_terminal = ports.cancel_legacy(key)
        if needs_terminal:
            await ports.bounded(
                ports.publish_terminal(key, legacy=True),
                deadline,
                "broadcast_legacy_abort_terminal",
                None,
            )
        return {
            "aborted": cancelled or bool(compactions),
            "key": key,
            "cancelled_compactions": len(compactions),
        }

    async def _exact(
        self,
        key: str,
        task_id: str,
        tasks: tuple[asyncio.Task[int | None], asyncio.Task[int], asyncio.Task[int]],
        deadline: float,
    ) -> CancelTurnResult:
        ports = self._ports
        runtime, auxiliary, descendants = tasks
        unsupported = False
        try:
            count = await ports.observe(
                runtime,
                deadline,
                "cancel_requested_runtime_task",
                None,
            )
        except ExactCancellationUnavailableError:
            count, unsupported = 0, True
        if count is None:
            # Cancellation may have committed. Only a same-identity retry is safe.
            return {"aborted": False, "key": key, "reason": "task_cancel_unknown"}
        if count > 0:
            return {"aborted": True, "key": key}
        count += await ports.observe(
            auxiliary,
            deadline,
            "cancel_requested_task_auxiliary_work",
            0,
        )
        count += await ports.observe(
            descendants,
            deadline,
            "cancel_requested_task_descendants",
            0,
        )
        if unsupported:
            return {"aborted": False, "key": key, "reason": "task_scope_unsupported"}
        if count > 0:
            return {"aborted": True, "key": key}
        active = await ports.bounded(
            ports.active_task_ids(key),
            deadline,
            "classify_inactive_runtime_task",
            (),
        )
        return {
            "aborted": False,
            "key": key,
            "reason": "task_mismatch" if active and task_id not in active else "task_not_active",
        }

    async def _tree(
        self,
        key: str,
        source: str,
        compaction_count: int,
        deadline: float,
    ) -> CancelTurnResult:
        ports = self._ports
        processed: set[str] = set()
        requested_tasks: set[str] = set()
        cancelled_sessions: set[str] = set()
        cancelled_tasks = cancelled_groups = approvals = 0
        # Re-scan after drains: an in-flight child may have committed another spawn.
        for pass_index in range(self._timing.tree_passes):
            if pass_index > 0 and self._clock() >= deadline:
                log.warning(
                    "sessions.abort.tree_stabilization_deadline",
                    session_key=key,
                    passes_completed=pass_index,
                )
                break
            keys = await ports.bounded(
                ports.session_tree(key), deadline, "list_session_tree", (key,)
            )
            new_keys = [candidate for candidate in keys if candidate not in processed]
            drains: list[tuple[str, tuple[str, ...]]] = []
            cancelled_this_pass = 0
            for candidate in keys:
                if self._clock() >= deadline:
                    log.warning(
                        "sessions.abort.tree_iteration_deadline",
                        session_key=key,
                        processed_sessions=len(processed),
                    )
                    break
                first_visit = candidate in new_keys
                if first_visit:
                    processed.add(candidate)
                    cancelled_groups += await ports.bounded(
                        ports.cancel_completion(candidate),
                        deadline,
                        "cancel_background_completion",
                        0,
                    )
                    cancelled_groups += await ports.bounded(
                        ports.cancel_processes(candidate),
                        deadline,
                        "cancel_persisted_session_processes",
                        0,
                    )
                active = await ports.bounded(
                    ports.active_task_ids(candidate),
                    deadline,
                    "list_runtime_tasks",
                    (),
                )
                new_tasks = tuple(task for task in active if task not in requested_tasks)
                if not first_visit and not new_tasks:
                    continue
                count = await ports.bounded(
                    ports.cancel_runtime(candidate, None, source),
                    deadline,
                    "cancel_runtime_tasks",
                    0,
                )
                cancelled_tasks += count
                cancelled_this_pass += count
                approvals += ports.reject_approvals(candidate)
                if count > 0:
                    requested_tasks.update(new_tasks)
                    cancelled_sessions.add(candidate)
                    drains.append((candidate, new_tasks))
            for candidate, task_ids in drains:
                await ports.drain(candidate, task_ids, deadline)
            if pass_index > 0 and not new_keys and cancelled_this_pass == 0:
                break
        else:
            log.warning(
                "sessions.abort.tree_stabilization_exhausted",
                session_key=key,
                passes=self._timing.tree_passes,
            )
        aborted = any((cancelled_tasks, cancelled_groups, approvals, compaction_count))
        if aborted:
            await ports.bounded(
                ports.publish_terminal(key, legacy=False),
                deadline,
                "broadcast_abort_terminal",
                None,
            )
        return {
            "aborted": aborted,
            "key": key,
            "cancelled_tasks": cancelled_tasks,
            "cancelled_sessions": len(cancelled_sessions),
            "cancelled_compactions": compaction_count,
        }
