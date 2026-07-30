"""Background completion delivery for subagent spawn groups."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import structlog

from opensquilla.channels.types import OutgoingMessage
from opensquilla.engine.types import done_text_snapshot
from opensquilla.session.models import AgentTaskStatus
from opensquilla.session.terminal_reply import sanitize_agent_error

log = structlog.get_logger(__name__)

EventEmitter = Callable[[str, str, dict[str, Any]], Awaitable[None]]
ChannelManagerRef = Callable[[], Any | None]


@dataclass(frozen=True)
class _DeliveryResult:
    status: str
    error_class: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class _DeliveryTarget:
    channel_name: str
    channel_id: str | None = None
    thread_id: str | None = None


class _SynthesisStreamCollector:
    """Collect the final assistant text emitted by one synthesis task."""

    def __init__(self) -> None:
        self._text_deltas: list[str] = []
        self._done_text: str | None = None

    async def __call__(self, event: Any) -> None:
        event_kind = _event_kind(event)
        if event_kind == "text_delta":
            text = _optional_str(_event_value(event, "text"))
            if text:
                self._text_deltas.append(text)
        elif event_kind == "done":
            snapshot_present, text = done_text_snapshot(event)
            if snapshot_present:
                self._done_text = text

    def text(self) -> str:
        if self._done_text is not None:
            return self._done_text
        return "".join(self._text_deltas)


class BackgroundCompletionManager:
    """Bridge completed subagent groups back to every user-facing entry point.

    The manager is intentionally process-local. It emits replayable
    ``session.event.task_group.*`` frames, schedules a parent synthesis turn,
    and best-effort delivers the final synthesized assistant text to the saved
    channel route.
    """

    def __init__(
        self,
        *,
        session_manager: Any,
        event_emitter: EventEmitter | None = None,
        channel_manager_ref: ChannelManagerRef | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._event_emitter = event_emitter
        self._channel_manager_ref = channel_manager_ref or (lambda: None)
        self._state_lock = asyncio.Lock()
        self._waiting_groups: set[str] = set()
        self._wake_groups: set[str] = set()
        self._delivery_attempted: set[str] = set()
        self._delivery_targets: dict[str, _DeliveryTarget] = {}
        self._parent_envelopes: dict[str, Any] = {}
        self._parent_run_mode_overrides: dict[str, Any] = {}
        self._cancelled_groups: set[str] = set()
        self._group_parents: dict[str, str] = {}
        self._watch_tasks: set[asyncio.Task[None]] = set()
        self._watch_task_owners: dict[asyncio.Task[None], tuple[str, str]] = {}
        self._parent_fence_refcounts: dict[str, int] = {}
        self._group_admissions: dict[str, dict[str, int]] = {}
        self._watch_state_changed = asyncio.Event()
        self._closing = False

    @staticmethod
    def group_id(parent_session_key: str, parent_task_id: str) -> str:
        return f"subagent:{parent_session_key}:{parent_task_id}"

    async def emit_waiting(
        self,
        *,
        parent_session_key: str,
        parent_task_id: str,
        pending_count: int | None = None,
    ) -> None:
        group_id = self.group_id(parent_session_key, parent_task_id)
        async with self._state_lock:
            self._group_parents[group_id] = parent_session_key
            if self._parent_fence_refcounts.get(parent_session_key, 0):
                self._cancel_group_locked(group_id)
                return
            if group_id in self._cancelled_groups:
                return
            if group_id in self._waiting_groups or group_id in self._wake_groups:
                return
            self._waiting_groups.add(group_id)
        payload = self._base_payload(
            parent_session_key=parent_session_key,
            parent_task_id=parent_task_id,
            status="waiting",
        )
        if pending_count is not None:
            payload["pending_count"] = pending_count
        await self._emit(parent_session_key, "waiting", payload)

    async def capture_delivery_target(
        self,
        *,
        parent_session_key: str,
        parent_task_id: str,
        task_runtime: Any,
    ) -> None:
        group_id = self.group_id(parent_session_key, parent_task_id)
        if not await self._begin_group_admission(parent_session_key, group_id):
            return
        try:
            parent_envelope = _parent_envelope_from_task_runtime(task_runtime, parent_task_id)
            parent_run_mode_override = _parent_run_mode_override_from_task_runtime(
                task_runtime,
                parent_task_id,
            )
            delivery_target = await _capture_delivery_target(
                session_manager=self._session_manager,
                task_runtime=task_runtime,
                parent_session_key=parent_session_key,
                parent_task_id=parent_task_id,
            )
            if parent_envelope is None and delivery_target is None:
                return
            async with self._state_lock:
                if self._closing:
                    return
                if self._parent_fence_refcounts.get(parent_session_key, 0):
                    self._group_parents[group_id] = parent_session_key
                    self._cancel_group_locked(group_id)
                    return
                if group_id in self._cancelled_groups:
                    return
                self._group_parents[group_id] = parent_session_key
                if parent_envelope is not None:
                    self._parent_envelopes.setdefault(group_id, parent_envelope)
                if parent_run_mode_override is not None:
                    self._parent_run_mode_overrides.setdefault(
                        group_id,
                        parent_run_mode_override,
                    )
                if delivery_target is not None:
                    self._delivery_targets.setdefault(group_id, delivery_target)
        finally:
            await self._end_group_admission(parent_session_key, group_id)

    async def cancel_session(self, parent_session_key: str) -> int:
        """Prevent existing subagent groups from waking an aborted parent."""
        async with self._state_lock:
            group_ids = self._group_ids_for_parent_sessions_locked((parent_session_key,))
            for group_id in group_ids:
                self._cancel_group_locked(group_id)
        return len(group_ids)

    @contextlib.asynccontextmanager
    async def quiesce_sessions(
        self,
        session_keys: Iterable[str],
    ) -> AsyncIterator[None]:
        """Fence and drain parent-wake watchers for an exact session batch.

        The refcount fence is installed before inspecting detached watchers, so
        nested callers remain fenced and new wake admissions are rejected. Any
        admission that started before the fence is allowed to finish its route
        lookup, observes the fence before registration, and is included in the
        stable drain. Unrelated parent watchers are never awaited.
        """

        keys = tuple(
            sorted(
                {
                    session_key
                    for session_key in session_keys
                    if isinstance(session_key, str) and session_key
                }
            )
        )
        if not keys:
            yield
            return

        async with self._state_lock:
            for session_key in keys:
                self._parent_fence_refcounts[session_key] = (
                    self._parent_fence_refcounts.get(session_key, 0) + 1
                )
            for group_id in self._group_ids_for_parent_sessions_locked(keys):
                self._cancel_group_locked(group_id)
            self._notify_watch_state_changed()

        try:
            await self._finish_quiesce_drain(keys)
            yield
        finally:
            await self._release_parent_fences(keys)

    async def active_group_ids(self, parent_session_key: str) -> list[str]:
        """Return the authoritative process-local groups keeping a parent active."""
        async with self._state_lock:
            return sorted(
                group_id
                for group_id in self._waiting_groups | self._wake_groups
                if self._group_parents.get(group_id) == parent_session_key
                and group_id not in self._cancelled_groups
            )

    async def active_run_mode_override(self, parent_session_key: str) -> Any | None:
        """Return the captured mode for one active parent completion group."""
        async with self._state_lock:
            active_group_ids = sorted(
                group_id
                for group_id in self._waiting_groups | self._wake_groups
                if self._group_parents.get(group_id) == parent_session_key
                and group_id not in self._cancelled_groups
            )
            for group_id in active_group_ids:
                override = self._parent_run_mode_overrides.get(group_id)
                if override is not None:
                    return override
        return None

    async def send_parent_wake(
        self,
        *,
        parent_session_key: str,
        parent_task_id: str,
        payloads: list[dict[str, Any]],
        task_runtime: Any,
        message: str,
        provenance: dict[str, Any],
    ) -> None:
        """Schedule a parent wake without waiting inline for same-session work."""
        group_id = self.group_id(parent_session_key, parent_task_id)
        if not await self._begin_group_admission(parent_session_key, group_id):
            return
        try:
            parent_envelope = _parent_envelope_from_task_runtime(task_runtime, parent_task_id)
            parent_run_mode_override = _parent_run_mode_override_from_task_runtime(
                task_runtime,
                parent_task_id,
            )
            delivery_target = await _capture_delivery_target(
                session_manager=self._session_manager,
                task_runtime=task_runtime,
                parent_session_key=parent_session_key,
                parent_task_id=parent_task_id,
            )
            async with self._state_lock:
                if self._closing:
                    return
                if self._parent_fence_refcounts.get(parent_session_key, 0):
                    self._group_parents[group_id] = parent_session_key
                    self._cancel_group_locked(group_id)
                    return
                if group_id in self._cancelled_groups:
                    return
                if group_id in self._wake_groups:
                    return
                self._group_parents[group_id] = parent_session_key
                self._wake_groups.add(group_id)
                self._waiting_groups.discard(group_id)
                parent_envelope = self._parent_envelopes.get(group_id) or parent_envelope
                if parent_envelope is not None:
                    self._parent_envelopes[group_id] = parent_envelope
                parent_run_mode_override = self._parent_run_mode_overrides.get(
                    group_id,
                    parent_run_mode_override,
                )
                if parent_run_mode_override is not None:
                    self._parent_run_mode_overrides[group_id] = parent_run_mode_override
                delivery_target = self._delivery_targets.get(group_id) or delivery_target
                if delivery_target is not None:
                    self._delivery_targets[group_id] = delivery_target
                task = asyncio.create_task(
                    self._enqueue_and_watch_parent_wake(
                        parent_session_key=parent_session_key,
                        parent_task_id=parent_task_id,
                        payloads=payloads,
                        task_runtime=task_runtime,
                        message=message,
                        provenance=provenance,
                        parent_envelope=parent_envelope,
                        parent_run_mode_override=parent_run_mode_override,
                        delivery_target=delivery_target,
                    )
                )
                self._watch_tasks.add(task)
                self._watch_task_owners[task] = (parent_session_key, group_id)
                self._notify_watch_state_changed()

            task.add_done_callback(self._discard_watch_task)
        finally:
            await self._end_group_admission(parent_session_key, group_id)

    async def drain(self, *, timeout: float | None = 30.0) -> None:
        deadline = None
        if timeout is not None:
            deadline = asyncio.get_running_loop().time() + timeout
        while True:
            tasks = await self._snapshot_watch_tasks()
            if not tasks:
                return
            wait_timeout = None
            if deadline is not None:
                wait_timeout = max(0.0, deadline - asyncio.get_running_loop().time())
                if wait_timeout <= 0:
                    log.warning("background_completion.drain_timeout", remaining=len(tasks))
                    return
            try:
                _done, pending = await asyncio.wait(tasks, timeout=wait_timeout)
            except ValueError:
                return
            if pending:
                log.warning(
                    "background_completion.drain_timeout",
                    remaining=len(pending),
                )
                return

    async def close(self, *, timeout: float | None = 30.0) -> None:
        async with self._state_lock:
            self._closing = True
        await self.drain(timeout=timeout)
        tasks = await self._snapshot_watch_tasks()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._state_lock:
            self._waiting_groups.clear()
            self._wake_groups.clear()
            self._delivery_attempted.clear()
            self._delivery_targets.clear()
            self._parent_envelopes.clear()
            self._parent_run_mode_overrides.clear()
            self._cancelled_groups.clear()
            self._group_parents.clear()
            self._watch_tasks.clear()
            self._watch_task_owners.clear()
            self._parent_fence_refcounts.clear()
            self._group_admissions.clear()
            self._notify_watch_state_changed()
            self._closing = True

    async def _snapshot_watch_tasks(self) -> list[asyncio.Task[None]]:
        async with self._state_lock:
            return [task for task in self._watch_tasks if not task.done()]

    async def _begin_group_admission(
        self,
        parent_session_key: str,
        group_id: str,
    ) -> bool:
        async with self._state_lock:
            if self._closing:
                return False
            if self._parent_fence_refcounts.get(parent_session_key, 0):
                self._group_parents[group_id] = parent_session_key
                self._cancel_group_locked(group_id)
                return False
            if group_id in self._cancelled_groups:
                return False
            groups = self._group_admissions.setdefault(parent_session_key, {})
            groups[group_id] = groups.get(group_id, 0) + 1
            self._notify_watch_state_changed()
            return True

    async def _end_group_admission(
        self,
        parent_session_key: str,
        group_id: str,
    ) -> None:
        async with self._state_lock:
            groups = self._group_admissions.get(parent_session_key)
            if groups is not None:
                remaining = groups.get(group_id, 0) - 1
                if remaining > 0:
                    groups[group_id] = remaining
                else:
                    groups.pop(group_id, None)
                if not groups:
                    self._group_admissions.pop(parent_session_key, None)
            self._notify_watch_state_changed()

    async def _finish_quiesce_drain(self, keys: tuple[str, ...]) -> None:
        drain = asyncio.create_task(self._cancel_and_drain_parent_watchers(keys))
        cancellation: asyncio.CancelledError | None = None
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
        if cancellation is not None:
            with contextlib.suppress(BaseException):
                drain.result()
            raise cancellation
        drain.result()

    async def _cancel_and_drain_parent_watchers(
        self,
        keys: tuple[str, ...],
    ) -> None:
        key_set = frozenset(keys)
        while True:
            async with self._state_lock:
                for group_id in self._group_ids_for_parent_sessions_locked(keys):
                    self._cancel_group_locked(group_id)
                tasks = {
                    task
                    for task, (parent_session_key, _group_id) in self._watch_task_owners.items()
                    if parent_session_key in key_set and not task.done()
                }
                admissions_active = any(
                    self._group_admissions.get(session_key) for session_key in keys
                )
                if not tasks and not admissions_active:
                    return
                state_changed = self._watch_state_changed

            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                for task in tasks:
                    self._discard_watch_task(task)
                continue
            await state_changed.wait()

    async def _release_parent_fences(self, keys: tuple[str, ...]) -> None:
        async with self._state_lock:
            for session_key in keys:
                remaining = self._parent_fence_refcounts.get(session_key, 0) - 1
                if remaining > 0:
                    self._parent_fence_refcounts[session_key] = remaining
                else:
                    self._parent_fence_refcounts.pop(session_key, None)
            self._notify_watch_state_changed()

    def _discard_watch_task(self, task: asyncio.Task[None]) -> None:
        self._watch_tasks.discard(task)
        self._watch_task_owners.pop(task, None)
        self._notify_watch_state_changed()

    def _notify_watch_state_changed(self) -> None:
        """Broadcast one state generation without a shared Event.clear race."""
        changed = self._watch_state_changed
        self._watch_state_changed = asyncio.Event()
        changed.set()

    def _group_ids_for_parent_sessions_locked(
        self,
        session_keys: Iterable[str],
    ) -> set[str]:
        key_set = frozenset(session_keys)
        candidates = {
            *self._waiting_groups,
            *self._wake_groups,
            *self._delivery_targets,
            *self._parent_envelopes,
            *self._parent_run_mode_overrides,
        }
        group_ids = {
            group_id for group_id in candidates if self._group_parents.get(group_id) in key_set
        }
        group_ids.update(
            group_id
            for parent_session_key, groups in self._group_admissions.items()
            if parent_session_key in key_set
            for group_id in groups
        )
        group_ids.update(
            group_id
            for parent_session_key, group_id in self._watch_task_owners.values()
            if parent_session_key in key_set
        )
        return group_ids

    def _cancel_group_locked(self, group_id: str) -> None:
        self._cancelled_groups.add(group_id)
        self._waiting_groups.discard(group_id)
        self._wake_groups.discard(group_id)
        self._delivery_attempted.discard(group_id)
        self._delivery_targets.pop(group_id, None)
        self._parent_envelopes.pop(group_id, None)
        self._parent_run_mode_overrides.pop(group_id, None)

    def _base_payload(
        self,
        *,
        parent_session_key: str,
        parent_task_id: str,
        status: str,
    ) -> dict[str, Any]:
        return {
            "group_id": self.group_id(parent_session_key, parent_task_id),
            "parent_session_key": parent_session_key,
            "parent_task_id": parent_task_id,
            "status": status,
        }

    async def _enqueue_and_watch_parent_wake(
        self,
        *,
        parent_session_key: str,
        parent_task_id: str,
        payloads: list[dict[str, Any]],
        task_runtime: Any,
        message: str,
        provenance: dict[str, Any],
        parent_envelope: Any | None,
        parent_run_mode_override: Any | None,
        delivery_target: _DeliveryTarget | None,
    ) -> None:
        group_id = self.group_id(parent_session_key, parent_task_id)
        try:
            await self._run_parent_wake(
                parent_session_key=parent_session_key,
                parent_task_id=parent_task_id,
                payloads=payloads,
                task_runtime=task_runtime,
                message=message,
                provenance=provenance,
                parent_envelope=parent_envelope,
                parent_run_mode_override=parent_run_mode_override,
                delivery_target=delivery_target,
            )
        finally:
            await self._evict_group(group_id)

    async def _run_parent_wake(
        self,
        *,
        parent_session_key: str,
        parent_task_id: str,
        payloads: list[dict[str, Any]],
        task_runtime: Any,
        message: str,
        provenance: dict[str, Any],
        parent_envelope: Any | None,
        parent_run_mode_override: Any | None,
        delivery_target: _DeliveryTarget | None,
    ) -> None:
        group_id = self.group_id(parent_session_key, parent_task_id)
        stream_collector = _SynthesisStreamCollector()
        try:
            await self._wait_for_parent_task_to_release(task_runtime, parent_task_id)
            async with self._state_lock:
                if group_id in self._cancelled_groups:
                    return
            send_with_envelope = getattr(task_runtime, "send_with_envelope", None)
            if parent_envelope is not None and callable(send_with_envelope):
                handle = await send_with_envelope(
                    parent_envelope,
                    message,
                    provenance=provenance,
                    stream_event_sink=stream_collector,
                    accepted_run_mode_override=parent_run_mode_override,
                )
            else:
                handle = await task_runtime.send(
                    parent_session_key,
                    message,
                    provenance=provenance,
                    stream_event_sink=stream_collector,
                )
        except Exception as exc:  # noqa: BLE001 - failure becomes a group event.
            error_class, error_message = sanitize_agent_error(
                {
                    "status": "failed",
                    "terminal_reason": "error",
                    "error_class": type(exc).__name__,
                    "error_message": str(exc),
                },
                fallback_error_class=type(exc).__name__,
                fallback_error_message=str(exc) or "Agent error",
            )
            await self._emit_terminal_failure(
                parent_session_key=parent_session_key,
                parent_task_id=parent_task_id,
                synthesis_task_id=None,
                error_class=error_class,
                error_message=error_message,
            )
            async with self._state_lock:
                self._wake_groups.discard(group_id)
            return

        synthesis_task_id = getattr(handle, "task_id", None)
        payload = self._base_payload(
            parent_session_key=parent_session_key,
            parent_task_id=parent_task_id,
            status="synthesizing",
        )
        if isinstance(synthesis_task_id, str) and synthesis_task_id:
            payload["synthesis_task_id"] = synthesis_task_id
        payload["child_count"] = len(payloads)
        await self._emit(parent_session_key, "synthesizing", payload)

        if not isinstance(synthesis_task_id, str) or not synthesis_task_id:
            await self._emit_terminal_failure(
                parent_session_key=parent_session_key,
                parent_task_id=parent_task_id,
                synthesis_task_id=None,
                error_class="MissingTaskHandle",
                error_message="parent wake did not return a synthesis task id",
            )
            return

        await self._watch_parent_synthesis(
            parent_session_key=parent_session_key,
            parent_task_id=parent_task_id,
            synthesis_task_id=synthesis_task_id,
            final_text=stream_collector.text,
            delivery_target=delivery_target,
            task_runtime=task_runtime,
        )

    async def _wait_for_parent_task_to_release(
        self,
        task_runtime: Any,
        parent_task_id: str,
    ) -> None:
        wait = getattr(task_runtime, "wait", None)
        if not callable(wait):
            return
        try:
            maybe_result = wait(parent_task_id)
            if inspect.isawaitable(maybe_result):
                await maybe_result
        except Exception:
            log.debug(
                "background_completion.parent_task_wait_failed",
                parent_task_id=parent_task_id,
            )

    async def _watch_parent_synthesis(
        self,
        *,
        parent_session_key: str,
        parent_task_id: str,
        synthesis_task_id: str,
        final_text: Callable[[], str],
        delivery_target: _DeliveryTarget | None,
        task_runtime: Any,
    ) -> None:
        try:
            record = await task_runtime.wait(synthesis_task_id)
        except Exception as exc:  # noqa: BLE001 - failure becomes a group event.
            error_class, error_message = sanitize_agent_error(
                {
                    "status": "failed",
                    "terminal_reason": "error",
                    "error_class": type(exc).__name__,
                    "error_message": str(exc),
                },
                fallback_error_class=type(exc).__name__,
                fallback_error_message=str(exc) or "Agent error",
            )
            await self._emit_terminal_failure(
                parent_session_key=parent_session_key,
                parent_task_id=parent_task_id,
                synthesis_task_id=synthesis_task_id,
                error_class=error_class,
                error_message=error_message,
            )
            return

        synthesis_status = _status_value(getattr(record, "status", None))
        async with self._state_lock:
            if self.group_id(parent_session_key, parent_task_id) in self._cancelled_groups:
                return
        if synthesis_status != AgentTaskStatus.SUCCEEDED.value:
            await self._emit_terminal_failure(
                parent_session_key=parent_session_key,
                parent_task_id=parent_task_id,
                synthesis_task_id=synthesis_task_id,
                synthesis_status=synthesis_status,
                error_class=_optional_str(getattr(record, "error_class", None)),
                error_message=_optional_str(getattr(record, "error_message", None)),
            )
            return

        delivery = await self._deliver_channel_final(
            group_id=self.group_id(parent_session_key, parent_task_id),
            parent_session_key=parent_session_key,
            content=final_text(),
            delivery_target=delivery_target,
        )
        payload = self._base_payload(
            parent_session_key=parent_session_key,
            parent_task_id=parent_task_id,
            status="done",
        )
        payload.update(
            {
                "synthesis_task_id": synthesis_task_id,
                "synthesis_status": synthesis_status,
                "delivery_status": delivery.status,
            }
        )
        if delivery.error_class:
            payload["delivery_error_class"] = delivery.error_class
        if delivery.error_message:
            payload["delivery_error_message"] = delivery.error_message
        await self._emit(parent_session_key, "done", payload)
        await self._evict_group(self.group_id(parent_session_key, parent_task_id))

    async def _emit_terminal_failure(
        self,
        *,
        parent_session_key: str,
        parent_task_id: str,
        synthesis_task_id: str | None,
        error_class: str | None = None,
        error_message: str | None = None,
        synthesis_status: str | None = None,
    ) -> None:
        payload = self._base_payload(
            parent_session_key=parent_session_key,
            parent_task_id=parent_task_id,
            status="failed",
        )
        if synthesis_task_id:
            payload["synthesis_task_id"] = synthesis_task_id
        if synthesis_status:
            payload["synthesis_status"] = synthesis_status
        if error_class:
            payload["error_class"] = error_class
        if error_message:
            payload["error_message"] = error_message
        payload["delivery_status"] = "not_applicable"
        await self._emit(parent_session_key, "failed", payload)
        await self._evict_group(self.group_id(parent_session_key, parent_task_id))

    async def _deliver_channel_final(
        self,
        *,
        group_id: str,
        parent_session_key: str,
        content: str,
        delivery_target: _DeliveryTarget | None,
    ) -> _DeliveryResult:
        if not content:
            return _DeliveryResult("not_applicable")

        if delivery_target is None:
            return _DeliveryResult("not_applicable")
        channel_name = delivery_target.channel_name
        channel_id = delivery_target.channel_id
        thread_id = delivery_target.thread_id
        if not channel_name or not (channel_id or thread_id):
            return _DeliveryResult("not_applicable")

        async with self._state_lock:
            if group_id in self._delivery_attempted:
                return _DeliveryResult("sent")
            self._delivery_attempted.add(group_id)

        channel_manager = self._channel_manager_ref()
        get_channel = getattr(channel_manager, "get", None)
        if not callable(get_channel):
            return _DeliveryResult(
                "failed",
                "ChannelManagerUnavailable",
                "channel manager is unavailable",
            )
        adapter = get_channel(channel_name)
        if adapter is None:
            return _DeliveryResult(
                "failed",
                "ChannelUnavailable",
                f"channel {channel_name!r} is unavailable",
            )

        message = _build_channel_message(
            channel_name=channel_name,
            channel_id=channel_id,
            thread_id=thread_id,
            content=content,
        )
        try:
            await adapter.send(message)
        except Exception as exc:  # noqa: BLE001 - delivery failure is reported, not raised.
            return _DeliveryResult("failed", type(exc).__name__, str(exc))
        return _DeliveryResult("sent")

    async def _emit(self, session_key: str, phase: str, payload: dict[str, Any]) -> None:
        if self._event_emitter is None:
            return
        try:
            await self._event_emitter(session_key, f"session.event.task_group.{phase}", payload)
        except Exception:
            log.debug(
                "background_completion.emit_failed",
                session_key=session_key,
                phase=phase,
            )

    async def _evict_group(self, group_id: str) -> None:
        async with self._state_lock:
            current_task = asyncio.current_task()
            if any(
                task is not current_task and owner_group_id == group_id and not task.done()
                for task, (_parent_session_key, owner_group_id) in (self._watch_task_owners.items())
            ):
                return
            if any(groups.get(group_id, 0) > 0 for groups in self._group_admissions.values()):
                return
            self._waiting_groups.discard(group_id)
            self._wake_groups.discard(group_id)
            self._delivery_attempted.discard(group_id)
            self._delivery_targets.pop(group_id, None)
            self._parent_envelopes.pop(group_id, None)
            self._parent_run_mode_overrides.pop(group_id, None)
            if group_id not in self._cancelled_groups:
                self._group_parents.pop(group_id, None)


async def _get_session(session_manager: Any, session_key: str) -> Any | None:
    get_session = getattr(session_manager, "get_session", None)
    if not callable(get_session):
        return None
    try:
        return await get_session(session_key)
    except Exception:
        return None


def _parent_envelope_from_task_runtime(task_runtime: Any, parent_task_id: str) -> Any | None:
    tasks = getattr(task_runtime, "_tasks", None)
    runtime_task = tasks.get(parent_task_id) if isinstance(tasks, dict) else None
    return getattr(runtime_task, "envelope", None)


def _parent_run_mode_override_from_task_runtime(
    task_runtime: Any,
    parent_task_id: str,
) -> Any | None:
    tasks = getattr(task_runtime, "_tasks", None)
    runtime_task = tasks.get(parent_task_id) if isinstance(tasks, dict) else None
    return getattr(runtime_task, "accepted_run_mode_override", None)


async def _capture_delivery_target(
    *,
    session_manager: Any,
    task_runtime: Any,
    parent_session_key: str,
    parent_task_id: str,
) -> _DeliveryTarget | None:
    return _delivery_target_from_task_runtime(
        task_runtime,
        parent_task_id,
    ) or await _delivery_target_from_session(session_manager, parent_session_key)


def _delivery_target_from_task_runtime(
    task_runtime: Any,
    parent_task_id: str,
) -> _DeliveryTarget | None:
    tasks = getattr(task_runtime, "_tasks", None)
    runtime_task = tasks.get(parent_task_id) if isinstance(tasks, dict) else None
    envelope = getattr(runtime_task, "envelope", None)
    if envelope is None:
        return None
    reply_target = getattr(envelope, "reply_target", None)
    if getattr(reply_target, "kind", None) == "channel":
        return _delivery_target_from_fields(
            channel_name=getattr(reply_target, "channel_name", None),
            channel_id=getattr(reply_target, "to", None),
            thread_id=getattr(reply_target, "thread_id", None),
        )
    return _delivery_target_from_fields(
        channel_name=getattr(envelope, "channel_name", None),
        channel_id=getattr(envelope, "channel_id", None),
        thread_id=getattr(envelope, "thread_id", None),
    )


async def _delivery_target_from_session(
    session_manager: Any,
    parent_session_key: str,
) -> _DeliveryTarget | None:
    parent = await _get_session(session_manager, parent_session_key)
    return _delivery_target_from_fields(
        channel_name=getattr(parent, "last_channel", None),
        channel_id=getattr(parent, "last_to", None),
        thread_id=getattr(parent, "last_thread_id", None),
    )


def _delivery_target_from_fields(
    *,
    channel_name: Any,
    channel_id: Any,
    thread_id: Any,
) -> _DeliveryTarget | None:
    name = _optional_str(channel_name)
    to = _optional_str(channel_id)
    thread = _optional_str(thread_id)
    if not name or not (to or thread):
        return None
    return _DeliveryTarget(channel_name=name, channel_id=to, thread_id=thread)


def _build_channel_message(
    *,
    channel_name: str,
    channel_id: str | None,
    thread_id: str | None,
    content: str,
) -> OutgoingMessage:
    if channel_name == "slack":
        metadata = {"channel": channel_id} if channel_id else {}
        if thread_id:
            return OutgoingMessage(content=content, reply_to=thread_id, metadata=metadata)
        if channel_id:
            return OutgoingMessage(
                content=content,
                reply_to=None,
                metadata={**metadata, "thread_ts": None},
            )
    return OutgoingMessage(content=content, reply_to=thread_id or channel_id)


def _status_value(value: Any) -> str | None:
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) and raw else None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _event_value(event: Any, key: str) -> Any:
    if isinstance(event, dict):
        if key in event:
            return event.get(key)
        payload = event.get("payload")
        if isinstance(payload, dict):
            return payload.get(key)
        return None
    return getattr(event, key, None)


def _event_kind(event: Any) -> str:
    raw = _event_value(event, "kind") or _event_value(event, "event") or _event_value(event, "type")
    raw_text = raw if isinstance(raw, str) and raw else event.__class__.__name__
    if raw_text.startswith("session.event."):
        raw_text = raw_text.removeprefix("session.event.")
    if raw_text.endswith("Event"):
        raw_text = raw_text.removesuffix("Event")
    return raw_text.lower()
