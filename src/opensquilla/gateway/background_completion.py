"""Background completion delivery for subagent spawn groups."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
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
        self._cancelled_groups: set[str] = set()
        self._watch_tasks: set[asyncio.Task[None]] = set()
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
        parent_envelope = _parent_envelope_from_task_runtime(task_runtime, parent_task_id)
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
            if group_id in self._cancelled_groups:
                return
            if parent_envelope is not None:
                self._parent_envelopes.setdefault(group_id, parent_envelope)
            if delivery_target is not None:
                self._delivery_targets.setdefault(group_id, delivery_target)

    async def cancel_session(self, parent_session_key: str) -> int:
        """Prevent existing subagent groups from waking an aborted parent."""
        prefix = f"subagent:{parent_session_key}:"
        async with self._state_lock:
            group_ids = {
                group_id
                for group_id in (
                    *self._waiting_groups,
                    *self._wake_groups,
                    *self._delivery_targets,
                    *self._parent_envelopes,
                )
                if group_id.startswith(prefix)
            }
            self._cancelled_groups.update(group_ids)
            for group_id in group_ids:
                self._waiting_groups.discard(group_id)
                self._wake_groups.discard(group_id)
                self._delivery_attempted.discard(group_id)
                self._delivery_targets.pop(group_id, None)
                self._parent_envelopes.pop(group_id, None)
        return len(group_ids)

    async def active_group_ids(self, parent_session_key: str) -> list[str]:
        """Return the authoritative process-local groups keeping a parent active."""
        prefix = f"subagent:{parent_session_key}:"
        async with self._state_lock:
            return sorted(
                group_id
                for group_id in self._waiting_groups | self._wake_groups
                if group_id.startswith(prefix) and group_id not in self._cancelled_groups
            )

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
        parent_envelope = _parent_envelope_from_task_runtime(task_runtime, parent_task_id)
        delivery_target = await _capture_delivery_target(
            session_manager=self._session_manager,
            task_runtime=task_runtime,
            parent_session_key=parent_session_key,
            parent_task_id=parent_task_id,
        )
        task: asyncio.Task[None] | None = None
        async with self._state_lock:
            if self._closing:
                return
            if group_id in self._cancelled_groups:
                return
            if group_id in self._wake_groups:
                return
            self._wake_groups.add(group_id)
            self._waiting_groups.discard(group_id)
            parent_envelope = self._parent_envelopes.get(group_id) or parent_envelope
            if parent_envelope is not None:
                self._parent_envelopes[group_id] = parent_envelope
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
                    delivery_target=delivery_target,
                )
            )
            self._watch_tasks.add(task)

        task.add_done_callback(self._watch_tasks.discard)

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
            self._cancelled_groups.clear()
            self._watch_tasks.clear()
            self._closing = True

    async def _snapshot_watch_tasks(self) -> list[asyncio.Task[None]]:
        async with self._state_lock:
            return [task for task in self._watch_tasks if not task.done()]

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
            self._waiting_groups.discard(group_id)
            self._wake_groups.discard(group_id)
            self._delivery_attempted.discard(group_id)
            self._delivery_targets.pop(group_id, None)
            self._parent_envelopes.pop(group_id, None)


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
