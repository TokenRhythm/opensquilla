"""Shared cancellation settlement primitives for turn-owned tasks."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal, cast

import structlog

CancellationPolicy = Literal["bounded", "must_settle"]

# User-initiated Stop should feel immediate. Automatic deadlines retain a
# longer cleanup opportunity because nobody is synchronously waiting to regain
# control of the composer.
STOP_CANCEL_GRACE_SECONDS = 0.25
TIMEOUT_CANCEL_GRACE_SECONDS = 5.0

# Backward-compatible name used by stream wrappers and diagnostics. Generic
# cleanup defaults to timeout behavior; Stop call sites pass the short grace
# explicitly.
CANCEL_GRACE_SECONDS = TIMEOUT_CANCEL_GRACE_SECONDS

_BACKGROUND_TASKS: set[asyncio.Future[object]] = set()
log = structlog.get_logger(__name__)


def _consume_background_result(
    task: asyncio.Future[object],
    *,
    operation: str,
    parked_at: float,
) -> None:
    _BACKGROUND_TASKS.discard(task)
    outcome = "completed"
    try:
        if task.exception() is not None:
            outcome = "failed"
    except asyncio.CancelledError:
        outcome = "cancelled"
    except BaseException:
        outcome = "failed"
    log.info(
        "cancellation.background_task_settled",
        operation=operation,
        outcome=outcome,
        duration_ms=int((time.monotonic() - parked_at) * 1000),
        active_background_tasks=len(_BACKGROUND_TASKS),
    )


def park_background_task[T](
    task: asyncio.Future[T],
    *,
    operation: str,
) -> None:
    """Keep a strong reference to detached work and consume its final result."""
    object_task = cast("asyncio.Future[object]", task)
    if object_task in _BACKGROUND_TASKS:
        return
    _BACKGROUND_TASKS.add(object_task)
    parked_at = time.monotonic()
    log.warning(
        "cancellation.background_task_parked",
        operation=operation,
        policy="bounded",
        active_background_tasks=len(_BACKGROUND_TASKS),
    )
    object_task.add_done_callback(
        lambda done: _consume_background_result(
            done,
            operation=operation,
            parked_at=parked_at,
        )
    )


def defer_async_cleanup(
    task: asyncio.Future[object],
    cleanup: Callable[[], Awaitable[object]],
    *,
    operation: str,
) -> None:
    """Run *cleanup* once *task* settles without racing the in-flight task."""

    def _schedule(_done: asyncio.Future[object]) -> None:
        async def _run_cleanup() -> object:
            return await cleanup()

        cleanup_task: asyncio.Task[object] = asyncio.create_task(_run_cleanup())
        park_background_task(cleanup_task, operation=operation)

    task.add_done_callback(_schedule)


async def cancel_task[T](
    task: asyncio.Future[T],
    *,
    policy: CancellationPolicy,
    operation: str,
    grace_seconds: float | None = None,
) -> bool:
    """Cancel *task* according to *policy* and report whether it settled.

    ``bounded`` tasks are parked after the grace period. ``must_settle`` tasks
    absorb repeated caller cancellation until their effect boundary is known.
    A repeated caller cancellation is re-raised after required settlement.
    """
    if task.done():
        with contextlib.suppress(BaseException):
            task.exception()
        return True

    task.cancel()
    if policy == "bounded":
        grace = CANCEL_GRACE_SECONDS if grace_seconds is None else max(0.0, grace_seconds)
        try:
            async with asyncio.timeout(grace):
                await asyncio.shield(task)
        except TimeoutError:
            park_background_task(task, operation=operation)
            return False
        except asyncio.CancelledError:
            if task.done():
                with contextlib.suppress(BaseException):
                    task.exception()
                return True
            park_background_task(task, operation=operation)
            raise
        except Exception as exc:
            log.debug(
                "cancellation.task_settled_with_error",
                operation=operation,
                error_type=type(exc).__name__,
            )
        with contextlib.suppress(BaseException):
            task.exception()
        return True

    repeated_cancel: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.done():
                break
            if repeated_cancel is None:
                repeated_cancel = exc
        except BaseException:
            break
    with contextlib.suppress(BaseException):
        task.exception()
    if repeated_cancel is not None:
        raise repeated_cancel
    return True


async def cancel_tasks[T](
    tasks: Mapping[asyncio.Task[T], CancellationPolicy],
    *,
    operation: str,
    grace_seconds: float | None = None,
) -> None:
    """Cancel a task group while applying one shared grace period."""
    active = {task: policy for task, policy in tasks.items() if not task.done()}
    if not active:
        for task in tasks:
            with contextlib.suppress(BaseException):
                task.exception()
        return

    for task in active:
        task.cancel()

    repeated_cancel: asyncio.CancelledError | None = None
    bounded = {task for task, policy in active.items() if policy == "bounded"}
    required = {task for task, policy in active.items() if policy == "must_settle"}

    if bounded:
        grace = CANCEL_GRACE_SECONDS if grace_seconds is None else max(0.0, grace_seconds)
        waiter = asyncio.gather(*bounded, return_exceptions=True)
        try:
            async with asyncio.timeout(grace):
                await asyncio.shield(waiter)
            done = bounded
            pending: set[asyncio.Task[T]] = set()
        except TimeoutError:
            done = {task for task in bounded if task.done()}
            pending = bounded - done
        except asyncio.CancelledError as exc:
            repeated_cancel = exc
            done = {task for task in bounded if task.done()}
            pending = bounded - done
        for task in done:
            with contextlib.suppress(BaseException):
                task.exception()
        for task in pending:
            park_background_task(task, operation=operation)

    while required:
        done = {task for task in required if task.done()}
        for task in done:
            with contextlib.suppress(BaseException):
                task.exception()
        required -= done
        if not required:
            break
        waiter = asyncio.gather(*required, return_exceptions=True)
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError as exc:
            if repeated_cancel is None:
                repeated_cancel = exc
            continue

    if repeated_cancel is not None:
        raise repeated_cancel


# Compatibility alias for existing stream wrapper tests and diagnostics.
ORPHANED_TASKS = _BACKGROUND_TASKS
