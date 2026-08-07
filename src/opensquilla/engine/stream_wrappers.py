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
from collections.abc import AsyncIterator, Awaitable
from typing import cast

from .types import AgentEvent, RunHeartbeatEvent, ToolUseStartEvent

_STREAM_DONE = object()

# How long a wrapper waits for a cancelled upstream task to actually finish
# before giving up on it. An upstream whose cleanup blocks — a ``finally`` that
# awaits a dead socket, a retry loop that swallows ``CancelledError`` — would
# otherwise hold the timeout open forever, which is the exact stall the timeout
# exists to end.
_CANCEL_GRACE_SECONDS = 5.0

# Abandoned tasks are parked here so the event loop keeps a strong reference and
# cannot destroy them while they are still pending.
_ORPHANED_TASKS: set[asyncio.Task[object]] = set()


def _forget_orphan(task: asyncio.Task[object]) -> None:
    _ORPHANED_TASKS.discard(task)
    # Retrieve whatever it ended with so asyncio does not log it as never retrieved.
    with contextlib.suppress(BaseException):
        task.exception()


async def _settle_or_abandon(task: asyncio.Task[object]) -> None:
    """Cancel *task* and wait a bounded time for it to finish; else detach it.

    ``asyncio.wait_for`` awaits the cancellation it requests, so an upstream
    that never finishes cancelling makes the timeout itself unbounded. Waiting
    only ``_CANCEL_GRACE_SECONDS`` and then walking away keeps the caller's
    deadline real. The task is left running rather than awaited: it is already
    unresponsive, and the turn it belonged to is failing regardless.
    """
    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=_CANCEL_GRACE_SECONDS)
    if done:
        with contextlib.suppress(BaseException):
            task.exception()
        return
    _ORPHANED_TASKS.add(task)
    task.add_done_callback(_forget_orphan)


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
) -> AsyncIterator[AgentEvent]:
    """Raise ``TimeoutError`` if no event arrives within *timeout* seconds.

    The pending ``__anext__`` is driven as a task and waited on with
    :func:`asyncio.wait`, which reports the deadline without cancelling
    anything. ``asyncio.wait_for`` would instead cancel the task and then await
    that cancellation, so an upstream whose cleanup blocks kept the timeout
    pending forever and the turn hung with no event and no error.
    """
    aiter = stream.__aiter__()
    pending: asyncio.Task[AgentEvent] | None = None
    try:
        while True:
            pending = asyncio.ensure_future(cast("Awaitable[AgentEvent]", aiter.__anext__()))
            done, _still_waiting = await asyncio.wait({pending}, timeout=timeout)
            if not done:
                await _settle_or_abandon(cast("asyncio.Task[object]", pending))
                pending = None
                raise TimeoutError(f"Stream idle for more than {timeout}s")
            settled, pending = pending, None
            try:
                event = settled.result()
            except StopAsyncIteration:
                return
            yield event
    finally:
        if pending is not None:
            await _settle_or_abandon(cast("asyncio.Task[object]", pending))


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

    queue: asyncio.Queue[AgentEvent | Exception | object] = asyncio.Queue()
    started = time.monotonic()
    last_event_at = started
    driver = asyncio.create_task(_drain_stream(stream.__aiter__(), queue))

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
            if isinstance(item, Exception):
                raise item

            event = cast(AgentEvent, item)
            last_event_at = time.monotonic()
            yield event
    finally:
        # Bounded for the same reason as the idle timeout: awaiting the driver's
        # cancellation outright let a wedged upstream hang this wrapper's
        # cleanup, and so the caller closing the stream after a failure.
        await _settle_or_abandon(cast("asyncio.Task[object]", driver))


async def _drain_stream(
    aiter: AsyncIterator[AgentEvent],
    queue: asyncio.Queue[AgentEvent | Exception | object],
) -> None:
    try:
        async for event in aiter:
            await queue.put(event)
    except Exception as exc:
        await queue.put(exc)
    finally:
        await queue.put(_STREAM_DONE)

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
) -> AsyncIterator[AgentEvent]:
    """Compose stream wrappers around *stream*.

    Args:
        stream:       Source async iterator of AgentEvent.
        repair_json:  Enable JSON-repair wrapper (default True).
        idle_timeout: Seconds before TimeoutError; None disables (default 30.0).
        heartbeat_interval: Seconds between quiet-stream heartbeat events;
            None disables.
        trim_names:   Enable tool-name trim wrapper (default True).
    """
    if repair_json:
        stream = repair_json_stream(stream)
    if trim_names:
        stream = trim_tool_names_stream(stream)
    if idle_timeout is not None:
        stream = idle_timeout_stream(stream, idle_timeout)
    if heartbeat_interval is not None:
        stream = heartbeat_stream(
            stream,
            interval=heartbeat_interval,
            phase=heartbeat_phase,
            message=heartbeat_message,
        )
    return stream
