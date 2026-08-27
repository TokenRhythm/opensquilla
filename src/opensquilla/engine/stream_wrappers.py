"""Stream wrappers for agent event streams.

Async-generator wrappers:
  - repair_json_stream   — fix malformed JSON in tool-use arguments
  - idle_timeout_stream  — raise TimeoutError when no event arrives within N seconds
  - trim_tool_names_stream — strip whitespace from tool names
  - heartbeat_stream — emit non-persistent run heartbeats while upstream is quiet

Use wrap_stream() to compose the wrappers in one call.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import re
import time
from collections.abc import AsyncIterator
from typing import cast

from .cancellation import CANCEL_GRACE_SECONDS, ORPHANED_TASKS, cancel_task
from .types import AgentEvent, RunHeartbeatEvent, ToolUseStartEvent

_STREAM_DONE = object()
_PULL_NEXT = object()


@dataclasses.dataclass(frozen=True, slots=True)
class _StreamFailure:
    error: BaseException


# How long a wrapper waits for a cancelled upstream task to actually finish
# before giving up on it. An upstream whose cleanup blocks — a ``finally`` that
# awaits a dead socket, a retry loop that swallows ``CancelledError`` — would
# otherwise hold the timeout open forever, which is the exact stall the timeout
# exists to end.
_CANCEL_GRACE_SECONDS = CANCEL_GRACE_SECONDS

# Abandoned tasks are parked here so the event loop keeps a strong reference and
# cannot destroy them while they are still pending.
_ORPHANED_TASKS = ORPHANED_TASKS


async def _settle_or_abandon(
    task: asyncio.Task[object],
    stop_requested: asyncio.Event,
) -> None:
    """Cancel *task* and wait a bounded time for it to finish; else detach it.

    ``asyncio.wait_for`` awaits the cancellation it requests, so an upstream
    that never finishes cancelling makes the timeout itself unbounded. Waiting
    only ``_CANCEL_GRACE_SECONDS`` and then walking away keeps the caller's
    deadline real. The task is left running rather than awaited: it is already
    unresponsive, and the turn it belonged to is failing regardless.
    """
    stop_requested.set()
    await cancel_task(
        task,
        policy="bounded",
        operation="stream_wrapper_driver",
        grace_seconds=_CANCEL_GRACE_SECONDS,
    )


# ---------------------------------------------------------------------------
# JSON repair helpers
# ---------------------------------------------------------------------------


def _repair_json(s: str) -> str:
    """Best-effort repair of partially-formed JSON strings.

    Fixes:
    1. Trailing commas before } or ]  — ``{"k": "v",}`` → ``{"k": "v"}``
    2. Whitespace-only keys            — strips leading/trailing spaces in keys
    3. Unclosed braces / brackets      — appends missing closing chars
    """
    if not s or not s.strip():
        return s

    # 1. Remove trailing commas before closing braces/brackets
    s = re.sub(r",\s*([}\]])", r"\1", s)

    # 2. Strip whitespace from object keys  ("  key  ": …)
    s = re.sub(r'"(\s+)(.*?)(\s+)"(\s*:)', lambda m: f'"{m.group(2)}"{m.group(4)}', s)

    # 3. Close unclosed structures
    stack: list[str] = []
    in_string = False
    escape_next = False
    for ch in s:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()

    # Append missing closers in reverse order
    s = s + "".join(reversed(stack))

    return s


# ---------------------------------------------------------------------------
# Wrapper 1: JSON repair
# ---------------------------------------------------------------------------


async def repair_json_stream(
    stream: AsyncIterator[AgentEvent],
) -> AsyncIterator[AgentEvent]:
    """Yield events unchanged except ToolUseStartEvent whose tool_name carries
    repaired JSON — and any future event that grows an ``arguments`` str field."""
    async for event in stream:
        # ToolUseStartEvent does not carry arguments (those arrive as deltas),
        # but downstream code may attach them; handle generically via hasattr.
        arguments = getattr(event, "arguments", None)
        if isinstance(arguments, str):
            setattr(event, "arguments", _repair_json(arguments))
        yield event


# ---------------------------------------------------------------------------
# Wrapper 2: Idle timeout
# ---------------------------------------------------------------------------


async def idle_timeout_stream(
    stream: AsyncIterator[AgentEvent],
    timeout: float = 30.0,
    *,
    context_bound: bool | None = None,
) -> AsyncIterator[AgentEvent]:
    """Raise ``TimeoutError`` if no event arrives within *timeout* seconds.

    Context-bound turns have a canonical owner for inactivity, fallback, and
    control terminal transitions. The legacy wrapper remains available for
    older streams, but it cannot create a competing timeout transition for a
    context-bound stream.

    The upstream is advanced by a driver task and the deadline is applied to a
    queue read, which is always safe to cancel. Awaiting ``__anext__`` directly
    under ``asyncio.wait_for`` was not: on timeout it cancelled the upstream and
    then *awaited that cancellation*, so an upstream whose cleanup blocked kept
    the timeout pending forever and the turn hung with no event, no error and no
    log line.

    One driver for the whole stream — the shape ``heartbeat_stream`` already
    uses — rather than a task per event. A task per event would hand the
    upstream a fresh context copy and a fresh task identity every time, and the
    turn depends on both being stable: ``TurnRunner.run`` sets the session-lock
    owner before its first event and resets that token in a ``finally``, and
    ``notify_compaction`` registers ``asyncio.current_task()`` as the owner
    whose exit finalizes an in-flight compaction. The driver still waits to be
    asked, so the upstream advances one event per consumed event exactly as it
    did when this wrapper awaited ``__anext__`` inline.
    """
    if _is_context_bound_stream(stream, context_bound):
        async for event in stream:
            yield event
        return

    requests: asyncio.Queue[object] = asyncio.Queue()
    results: asyncio.Queue[AgentEvent | _StreamFailure | object] = asyncio.Queue()
    stop_requested = asyncio.Event()
    driver = asyncio.create_task(
        _pull_on_demand(stream.__aiter__(), requests, results, stop_requested)
    )
    try:
        while True:
            requests.put_nowait(_PULL_NEXT)
            try:
                item = await asyncio.wait_for(results.get(), timeout=timeout)
            except TimeoutError as exc:
                raise TimeoutError(f"Stream idle for more than {timeout}s") from exc
            if item is _STREAM_DONE:
                return
            if isinstance(item, _StreamFailure):
                raise item.error
            yield cast(AgentEvent, item)
    finally:
        # Bounded: a wedged upstream must not hold the deadline it just missed.
        await _settle_or_abandon(cast("asyncio.Task[object]", driver), stop_requested)


async def _pull_on_demand(
    aiter: AsyncIterator[AgentEvent],
    requests: asyncio.Queue[object],
    results: asyncio.Queue[AgentEvent | _StreamFailure | object],
    stop_requested: asyncio.Event,
) -> None:
    """Advance *aiter* once per request so the wrapper keeps pull semantics."""
    try:
        while not stop_requested.is_set():
            await requests.get()
            if stop_requested.is_set():
                return
            try:
                event = await aiter.__anext__()
            except StopAsyncIteration:
                results.put_nowait(_STREAM_DONE)
                return
            except asyncio.CancelledError as exc:
                if not stop_requested.is_set():
                    results.put_nowait(_StreamFailure(exc))
                raise
            except Exception as exc:
                if not stop_requested.is_set():
                    results.put_nowait(_StreamFailure(exc))
                return
            if stop_requested.is_set():
                return
            results.put_nowait(event)
    finally:
        # The consumer cannot close an iterator it never touches, and cancelling
        # this task while it waits to be asked would otherwise leave the
        # upstream's ``finally`` blocks to garbage collection.
        close = getattr(aiter, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()


# ---------------------------------------------------------------------------
# Wrapper 3: Run heartbeat while waiting
# ---------------------------------------------------------------------------


async def heartbeat_stream(
    stream: AsyncIterator[AgentEvent],
    *,
    interval: float = 15.0,
    phase: str = "agent",
    message: str = "Still working",
) -> AsyncIterator[AgentEvent]:
    """Emit ``RunHeartbeatEvent`` while waiting for the next upstream event.

    This wrapper does not cancel the pending upstream ``__anext__`` call when
    the heartbeat interval elapses. If the upstream stream is also wrapped by
    ``idle_timeout_stream``, the real timeout still propagates once reached.
    """
    if interval <= 0:
        async for event in stream:
            yield event
        return

    queue: asyncio.Queue[AgentEvent | _StreamFailure | object] = asyncio.Queue()
    started = time.monotonic()
    last_event_at = started
    stop_requested = asyncio.Event()
    driver = asyncio.create_task(_drain_stream(stream.__aiter__(), queue, stop_requested))

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                now = time.monotonic()
                yield RunHeartbeatEvent(
                    phase=phase,
                    elapsed_ms=int((now - started) * 1000),
                    idle_ms=int((now - last_event_at) * 1000),
                    message=message,
                )
                continue

            if item is _STREAM_DONE:
                return
            if isinstance(item, _StreamFailure):
                raise item.error

            event = cast(AgentEvent, item)
            last_event_at = time.monotonic()
            yield event
    finally:
        # Bounded for the same reason as the idle timeout: awaiting the driver's
        # cancellation outright let a wedged upstream hang this wrapper's
        # cleanup, and so the caller closing the stream after a failure.
        await _settle_or_abandon(cast("asyncio.Task[object]", driver), stop_requested)


async def _drain_stream(
    aiter: AsyncIterator[AgentEvent],
    queue: asyncio.Queue[AgentEvent | _StreamFailure | object],
    stop_requested: asyncio.Event,
) -> None:
    try:
        async for event in aiter:
            if stop_requested.is_set():
                return
            queue.put_nowait(event)
    except asyncio.CancelledError as exc:
        if not stop_requested.is_set():
            queue.put_nowait(_StreamFailure(exc))
        raise
    except Exception as exc:
        if not stop_requested.is_set():
            queue.put_nowait(_StreamFailure(exc))
    finally:
        try:
            # Natural exhaustion and ordinary failures retain their previous
            # iterator lifecycle. Only our early stop needs an explicit close.
            if stop_requested.is_set():
                close = getattr(aiter, "aclose", None)
                if close is not None:
                    with contextlib.suppress(Exception):
                        await close()
        finally:
            queue.put_nowait(_STREAM_DONE)


# ---------------------------------------------------------------------------
# Wrapper 4: Tool-name trim
# ---------------------------------------------------------------------------


async def trim_tool_names_stream(
    stream: AsyncIterator[AgentEvent],
) -> AsyncIterator[AgentEvent]:
    """Strip leading/trailing whitespace from tool names in ToolUseStartEvent."""
    async for event in stream:
        if isinstance(event, ToolUseStartEvent) and event.tool_name != event.tool_name.strip():
            event = dataclasses.replace(event, tool_name=event.tool_name.strip())
        yield event


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------


def wrap_stream(
    stream: AsyncIterator[AgentEvent],
    *,
    repair_json: bool = True,
    idle_timeout: float | None = 30.0,
    heartbeat_interval: float | None = None,
    heartbeat_phase: str = "agent",
    heartbeat_message: str = "Still working",
    trim_names: bool = True,
    context_bound: bool | None = None,
) -> AsyncIterator[AgentEvent]:
    """Compose stream wrappers around *stream*.

    Args:
        stream:       Source async iterator of AgentEvent.
        repair_json:  Enable JSON-repair wrapper (default True).
        idle_timeout: Seconds before TimeoutError; None disables (default 30.0).
        heartbeat_interval: Seconds between quiet-stream heartbeat events;
            None disables.
        trim_names:   Enable tool-name trim wrapper (default True).
        context_bound: If true, leave timeout ownership to the turn context.
            ``None`` also recognizes streams carrying an explicit
            ``context_bound`` or ``execution_context`` marker.  The default
            legacy stream behavior remains timeout-enforced.
    """
    context_bound = _is_context_bound_stream(stream, context_bound)
    if repair_json:
        stream = repair_json_stream(stream)
    if trim_names:
        stream = trim_tool_names_stream(stream)
    if idle_timeout is not None and not context_bound:
        stream = idle_timeout_stream(stream, idle_timeout, context_bound=False)
    if heartbeat_interval is not None:
        stream = heartbeat_stream(
            stream,
            interval=heartbeat_interval,
            phase=heartbeat_phase,
            message=heartbeat_message,
        )
    return stream


def _is_context_bound_stream(
    stream: object,
    context_bound: bool | None,
) -> bool:
    """Resolve the explicit context marker without changing legacy callers."""
    if context_bound is not None:
        return context_bound
    return bool(
        getattr(stream, "context_bound", False)
        or getattr(stream, "execution_context", None) is not None
    )


def is_context_bound_owner(owner: object | None) -> bool:
    """Recognize the built-in turn owner without changing legacy runners.

    ``TurnRunner.run`` is an async generator, so its execution context is
    created only when the stream is iterated and cannot be attached to the
    generator object at the gateway boundary.  The built-in runner exposes
    the two lifecycle methods below; injected legacy runners generally do not.
    Explicit markers remain the preferred extension seam for other owners.
    """
    if owner is None:
        return False
    if _is_context_bound_stream(owner, None):
        return True
    return callable(getattr(owner, "_run_turn", None)) and callable(
        getattr(owner, "get_session_lock", None)
    )
