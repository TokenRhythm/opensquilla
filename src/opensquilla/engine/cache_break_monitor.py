"""Prompt-cache break detection for cache-relevant provider request state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from opensquilla.provider import ChatConfig, Message, ToolDefinition

_MAX_TRACKED_SESSIONS = 512


def _jsonable(value: Any) -> Any:
    """Return a stable JSON-ish shape for Pydantic models and dataclasses."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _stable_hash(value: Any) -> str:
    payload = json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _message_prefix_messages(messages: list[Message], *, tail_count: int = 2) -> list[Message]:
    if tail_count <= 0:
        return list(messages)
    return list(messages[:-tail_count])


def _message_prefix_payload(messages: list[Message], *, tail_count: int = 2) -> list[Any]:
    return [
        _jsonable(message) for message in _message_prefix_messages(messages, tail_count=tail_count)
    ]


def _message_prefix_item_kind(message: Message) -> str:
    content = message.content
    if isinstance(content, str):
        if content.startswith("[Request context for this turn]"):
            return "request_context"
        if content.startswith("[Runtime context for this turn]"):
            return "runtime_context"
        if content.startswith("[Available skills for this turn]"):
            return "skills_context"
    return "history"


def _cache_control_payload(config: ChatConfig) -> dict[str, Any]:
    return {
        "cache_breakpoints": config.cache_breakpoints,
        "cache_mode": config.cache_mode,
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
        "stop_sequences": config.stop_sequences,
        "thinking": config.thinking,
        "thinking_budget_tokens": config.thinking_budget_tokens,
        "thinking_level": str(config.thinking_level) if config.thinking_level else None,
    }


@dataclass(frozen=True)
class PromptStateSnapshot:
    """Cache-relevant request inputs recorded immediately before a chat call."""

    system_hash: str
    tools_hash: str
    messages_prefix_hash: str
    cache_control_hash: str
    model: str
    message_count: int
    tool_count: int
    messages_prefix_item_hashes: tuple[str, ...] = ()
    messages_prefix_item_kinds: tuple[str, ...] = ()
    cache_control_field_hashes: tuple[tuple[str, str], ...] = ()

    def changed_fields(self, previous: PromptStateSnapshot) -> tuple[str, ...]:
        changed: list[str] = []
        for field_name in (
            "system_hash",
            "tools_hash",
            "messages_prefix_hash",
            "cache_control_hash",
            "model",
        ):
            if getattr(self, field_name) != getattr(previous, field_name):
                changed.append(field_name)
        return tuple(changed)

    def to_forensics(self) -> dict[str, Any]:
        return {
            "system_hash": self.system_hash,
            "tools_hash": self.tools_hash,
            "messages_prefix_hash": self.messages_prefix_hash,
            "messages_prefix_item_hashes": list(self.messages_prefix_item_hashes),
            "messages_prefix_item_kinds": list(self.messages_prefix_item_kinds),
            "cache_control_hash": self.cache_control_hash,
            "cache_control_field_hashes": dict(self.cache_control_field_hashes),
            "model": self.model,
            "message_count": self.message_count,
            "tool_count": self.tool_count,
        }


@dataclass(frozen=True)
class CacheBreakReport:
    """Result of comparing a provider DoneEvent with the previous cache baseline."""

    break_detected: bool
    reason: str
    changed_fields: tuple[str, ...] = ()
    previous_cache_read_tokens: int = 0
    current_cache_read_tokens: int = 0
    drop_tokens: int = 0
    drop_ratio: float = 0.0
    baseline_reset: bool = False
    previous_snapshot: PromptStateSnapshot | None = None
    current_snapshot: PromptStateSnapshot | None = None

    def to_log_dict(self) -> dict[str, Any]:
        payload = {
            "reason": self.reason,
            "changed_fields": list(self.changed_fields),
            "previous_cache_read_tokens": self.previous_cache_read_tokens,
            "current_cache_read_tokens": self.current_cache_read_tokens,
            "drop_tokens": self.drop_tokens,
            "drop_ratio": self.drop_ratio,
            "baseline_reset": self.baseline_reset,
        }
        if self.break_detected and self.previous_snapshot and self.current_snapshot:
            payload["forensics"] = {
                "previous": self.previous_snapshot.to_forensics(),
                "current": self.current_snapshot.to_forensics(),
            }
        return payload


@dataclass(frozen=True)
class _CacheBaseline:
    snapshot: PromptStateSnapshot
    cache_read_tokens: int


@dataclass(slots=True)
class _SessionCacheState:
    """Cache-break diagnostics retained for one reusable session key.

    Baseline and pending reset share one state object so capacity eviction can
    never leave a pre-compaction baseline behind after dropping the marker that
    suppresses its expected cache-read decline.
    """

    baseline: _CacheBaseline | None = None
    reset_pending: bool = False


class CacheBreakMonitor:
    """Track cache-read drops without retaining unbounded session state.

    Explicit session-identity disposal evicts state through
    ``SessionManager.evict_session_runtime_state``. The shared LRU is a
    backstop for reusable or abandoned session keys that do not cross that
    boundary. Eviction only loses diagnostics: the next response initializes a
    new baseline and cannot fabricate a cache-break report.
    """

    def __init__(
        self,
        *,
        min_drop_tokens: int = 2000,
        min_drop_ratio: float = 0.05,
        max_sessions: int = _MAX_TRACKED_SESSIONS,
    ) -> None:
        self._sessions: OrderedDict[str, _SessionCacheState] = OrderedDict()
        self._min_drop_tokens = max(0, int(min_drop_tokens))
        self._min_drop_ratio = max(0.0, float(min_drop_ratio))
        self._max_sessions = max(1, int(max_sessions))

    @property
    def tracked_session_count(self) -> int:
        """Return the number of session keys currently holding diagnostics."""

        return len(self._sessions)

    def _touch(self, session_key: str) -> _SessionCacheState:
        state = self._sessions.get(session_key)
        if state is None:
            state = _SessionCacheState()
            self._sessions[session_key] = state
        self._sessions.move_to_end(session_key)
        return state

    def _trim_to_max_sessions(self) -> None:
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)

    def record_prompt_state(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: ChatConfig,
        model: str,
    ) -> PromptStateSnapshot:
        messages_prefix_messages = _message_prefix_messages(messages)
        messages_prefix_payload = [_jsonable(message) for message in messages_prefix_messages]
        cache_control_payload = _cache_control_payload(config)
        return PromptStateSnapshot(
            system_hash=_stable_hash(config.system or ""),
            tools_hash=_stable_hash(tools or []),
            messages_prefix_hash=_stable_hash(messages_prefix_payload),
            cache_control_hash=_stable_hash(cache_control_payload),
            model=model,
            message_count=len(messages),
            tool_count=len(tools or []),
            messages_prefix_item_hashes=tuple(
                _stable_hash(message) for message in messages_prefix_payload
            ),
            messages_prefix_item_kinds=tuple(
                _message_prefix_item_kind(message) for message in messages_prefix_messages
            ),
            cache_control_field_hashes=tuple(
                (key, _stable_hash(value)) for key, value in sorted(cache_control_payload.items())
            ),
        )

    def check_response_for_cache_break(
        self,
        session_key: str,
        snapshot: PromptStateSnapshot,
        cache_read_tokens: int,
    ) -> CacheBreakReport:
        current_tokens = max(0, int(cache_read_tokens or 0))
        state = self._touch(session_key)
        previous = state.baseline
        reset_pending = state.reset_pending
        state.baseline = _CacheBaseline(snapshot, current_tokens)
        state.reset_pending = False
        self._trim_to_max_sessions()
        if reset_pending:
            return CacheBreakReport(
                break_detected=False,
                reason="baseline_reset_after_compaction",
                current_cache_read_tokens=current_tokens,
                baseline_reset=True,
            )
        if previous is None:
            return CacheBreakReport(
                break_detected=False,
                reason="baseline_initialized",
                current_cache_read_tokens=current_tokens,
            )

        drop_tokens = max(0, previous.cache_read_tokens - current_tokens)
        drop_ratio = drop_tokens / previous.cache_read_tokens if previous.cache_read_tokens else 0.0
        changed_fields = snapshot.changed_fields(previous.snapshot)
        break_detected = (
            bool(changed_fields)
            and drop_tokens >= self._min_drop_tokens
            and drop_ratio >= self._min_drop_ratio
        )
        return CacheBreakReport(
            break_detected=break_detected,
            reason="cache_read_drop" if break_detected else "cache_read_stable",
            changed_fields=changed_fields,
            previous_cache_read_tokens=previous.cache_read_tokens,
            current_cache_read_tokens=current_tokens,
            drop_tokens=drop_tokens,
            drop_ratio=round(drop_ratio, 4),
            previous_snapshot=previous.snapshot if break_detected else None,
            current_snapshot=snapshot if break_detected else None,
        )

    def notify_compaction(self, session_key: str) -> None:
        """Treat the next provider response for this session as a new baseline."""

        self._touch(session_key).reset_pending = True
        self._trim_to_max_sessions()

    def evict(self, session_key: str) -> bool:
        """Drop cache-break diagnostics for one retired session key."""

        return self._sessions.pop(session_key, None) is not None

    def clear(self) -> None:
        self._sessions.clear()


default_cache_break_monitor = CacheBreakMonitor()

_CompactionListener = Callable[[str, dict[str, Any]], None]
_compaction_listeners: list[_CompactionListener] = []
_COMPACTION_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "skipped",
        "failed",
        "error",
        "cancelled",
        "timed_out",
        "stale",
        "emergency_ephemeral",
    }
)
_COMPACTION_TERMINAL_CACHE_SIZE = 2048
_compaction_sequences: dict[str, int] = {}
_compaction_terminals: OrderedDict[str, str] = OrderedDict()
_active_compaction_tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}
_compaction_heartbeat_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}


def _finalize_compaction_when_owner_stops(
    session_key: str,
    compaction_id: str,
    source: str,
    phase: str,
    owner: asyncio.Task[Any],
) -> None:
    """Backstop an owner task that exits without publishing a terminal event.

    Known compaction branches still publish their precise terminal status.  This
    callback covers unexpected exceptions between those branches so a stopped
    owner cannot leave a heartbeat and busy UI behind.  The terminal claim is
    idempotent with the normal path.
    """

    key = (session_key, compaction_id)
    if _active_compaction_tasks.get(key) is not owner:
        return
    if compaction_id in _compaction_terminals:
        return

    if owner.cancelled():
        status = "cancelled"
        reason = "owner_task_cancelled"
    else:
        try:
            owner_error = owner.exception()
        except asyncio.CancelledError:
            status = "cancelled"
            reason = "owner_task_cancelled"
        else:
            status = "failed"
            reason = "owner_task_failed" if owner_error is not None else "terminal_missing"

    # Keep imports local: engine types import the lifecycle helpers while this
    # module is initialized.
    from opensquilla.session.compaction_lifecycle import (
        COMPACTION_TRIGGERED_EVENT,
        compaction_effect_payload,
        compaction_lifecycle_payload,
    )

    notify_compaction(
        session_key,
        source=source,
        phase=phase,
        status=status,
        reason=reason,
        track_current_task=False,
        **compaction_effect_payload(status=status, reason=reason),
        **compaction_lifecycle_payload(compaction_id, COMPACTION_TRIGGERED_EVENT),
    )


def add_compaction_listener(listener: _CompactionListener) -> Callable[[], None]:
    """Register a best-effort listener for successful compaction events."""

    _compaction_listeners.append(listener)

    def remove() -> None:
        try:
            _compaction_listeners.remove(listener)
        except ValueError:
            pass

    return remove


def record_prompt_state(
    *,
    messages: list[Message],
    tools: list[ToolDefinition] | None,
    config: ChatConfig,
    model: str,
) -> PromptStateSnapshot:
    return default_cache_break_monitor.record_prompt_state(
        messages=messages,
        tools=tools,
        config=config,
        model=model,
    )


def check_response_for_cache_break(
    session_key: str,
    snapshot: PromptStateSnapshot,
    cache_read_tokens: int,
) -> CacheBreakReport:
    return default_cache_break_monitor.check_response_for_cache_break(
        session_key,
        snapshot,
        cache_read_tokens,
    )


def evict_cache_break_state(session_key: str) -> bool:
    """Drop process-wide cache-break diagnostics for one session key."""

    return default_cache_break_monitor.evict(session_key)


def notify_compaction(
    session_key: str,
    *,
    notify_listeners: object = True,
    track_current_task: object = True,
    **payload: Any,
) -> dict[str, Any] | None:
    """Publish one idempotent lifecycle event and return its normalized payload.

    Terminal delivery is at-least-once at the transport boundary, but this
    process claims terminal state only once per compaction id.  The additive
    sequence lets reconnecting clients discard duplicate/out-of-order events.
    """

    event_payload = {
        "status": str(payload.pop("status", "completed") or "completed"),
        "source": str(payload.pop("source", "automatic") or "automatic"),
        **payload,
    }
    # TaskRuntime creates one turn context around the shared runner.  Reuse
    # that identity here so every compaction lifecycle frame can be correlated
    # with the live assistant activity without widening every compaction call.
    from opensquilla.session.turn_context import (
        append_current_turn_activity_marker,
        current_turn_context,
    )

    turn_context = current_turn_context()
    if turn_context is not None:
        turn_id = str(turn_context.get("turn_id") or turn_context.get("task_id") or "").strip()
        task_id = str(turn_context.get("task_id") or turn_id).strip()
        if turn_id:
            event_payload.setdefault("turn_id", turn_id)
        if task_id:
            event_payload.setdefault("task_id", task_id)
    status = str(event_payload["status"]).lower()
    source = str(event_payload["source"]).lower()
    compaction_id = str(
        event_payload.get("compaction_id")
        or event_payload.get("compactionId")
        or ""
    ).strip()
    if compaction_id:
        if compaction_id in _compaction_terminals:
            return None
        sequence = _compaction_sequences.get(compaction_id, 0) + 1
        _compaction_sequences[compaction_id] = sequence
        event_payload.setdefault("sequence", sequence)

        key = (session_key, compaction_id)
        if status == "started":
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if current is not None and bool(track_current_task):
                _active_compaction_tasks[key] = current
                current.add_done_callback(
                    partial(
                        _finalize_compaction_when_owner_stops,
                        session_key,
                        compaction_id,
                        str(event_payload.get("source") or "automatic"),
                        str(event_payload.get("phase") or "compaction"),
                    )
                )
            interval = event_payload.get("heartbeat_interval_seconds")
            try:
                heartbeat_interval = float(interval) if interval is not None else 0.0
            except (TypeError, ValueError):
                heartbeat_interval = 0.0
            if (
                heartbeat_interval > 0
                and bool(notify_listeners)
                and current is not None
            ):
                existing = _compaction_heartbeat_tasks.get(key)
                if existing is None or existing.done():
                    _compaction_heartbeat_tasks[key] = asyncio.create_task(
                        _compaction_heartbeat_loop(
                            session_key=session_key,
                            compaction_id=compaction_id,
                            source=str(event_payload.get("source") or "automatic"),
                            phase=str(event_payload.get("phase") or "compaction"),
                            interval=heartbeat_interval,
                        )
                    )
        if status in _COMPACTION_TERMINAL_STATUSES:
            _compaction_terminals[compaction_id] = status
            _compaction_terminals.move_to_end(compaction_id)
            while len(_compaction_terminals) > _COMPACTION_TERMINAL_CACHE_SIZE:
                expired_id, _ = _compaction_terminals.popitem(last=False)
                _compaction_sequences.pop(expired_id, None)
            _active_compaction_tasks.pop(key, None)
            heartbeat_task = _compaction_heartbeat_tasks.pop(key, None)
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if heartbeat_task is not None and heartbeat_task is not current:
                heartbeat_task.cancel()

    if (
        compaction_id
        and source == "automatic"
        and status == "completed"
        and event_payload.get("applied") is True
        and str(event_payload.get("durability") or "").lower() == "durable"
    ):
        append_current_turn_activity_marker(
            {
                "kind": "context_compaction",
                "id": compaction_id,
                "status": "completed",
                "at": time.time_ns() // 1_000_000,
            }
        )

    if status == "completed":
        default_cache_break_monitor.notify_compaction(session_key)
    if not bool(notify_listeners):
        return event_payload
    for listener in tuple(_compaction_listeners):
        try:
            listener(session_key, dict(event_payload))
        except Exception:
            pass
    return event_payload


async def _compaction_heartbeat_loop(
    *,
    session_key: str,
    compaction_id: str,
    source: str,
    phase: str,
    interval: float,
) -> None:
    started = time.monotonic()
    try:
        while True:
            await asyncio.sleep(interval)
            accepted = notify_compaction(
                session_key,
                source=source,
                phase=phase,
                status="observed",
                compaction_id=compaction_id,
                heartbeat=True,
                heartbeat_at=int(time.time() * 1000),
                elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
            if accepted is None:
                return
    except asyncio.CancelledError:
        return


def compaction_terminal_status(compaction_id: str) -> str | None:
    """Return the terminal state claimed in this process, if any."""

    return _compaction_terminals.get(str(compaction_id or ""))


def active_compaction_ids(session_key: str) -> tuple[str, ...]:
    """Return live operation ids for one session."""

    return tuple(
        compaction_id
        for (candidate_key, compaction_id), task in _active_compaction_tasks.items()
        if candidate_key == session_key and not task.done()
    )


def register_active_compaction(
    session_key: str,
    compaction_id: str,
    task: asyncio.Task[Any],
) -> None:
    """Register a background manual operation before it acquires session lock."""

    if task.done():
        return
    _active_compaction_tasks[(session_key, compaction_id)] = task


def cancel_active_compactions(session_key: str) -> tuple[asyncio.Task[Any], ...]:
    """Request cancellation for every live compaction in one session."""

    tasks: list[asyncio.Task[Any]] = []
    for (candidate_key, _), task in tuple(_active_compaction_tasks.items()):
        if candidate_key != session_key or task.done() or task in tasks:
            continue
        task.cancel()
        tasks.append(task)
    return tuple(tasks)
