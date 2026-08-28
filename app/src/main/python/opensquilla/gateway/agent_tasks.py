"""Registry for tracking running agent tasks per session."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable

import structlog

from opensquilla.session.keys import canonicalize_session_key

log = structlog.get_logger(__name__)


class AgentTaskRegistry:
    """Track running agent tasks per session for abort/status queries."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._unsettled_tasks: dict[str, set[asyncio.Task]] = {}
        self._cleanup_tasks: dict[str, set[asyncio.Task[None]]] = {}
        self._admission_locks: dict[str, asyncio.Lock] = {}

    @contextlib.asynccontextmanager
    async def admission(self, session_key: str) -> AsyncIterator[None]:
        """Serialize durable direct acceptance through task registration."""

        key = canonicalize_session_key(session_key)
        lock = self._admission_locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield

    @contextlib.asynccontextmanager
    async def quiesce_sessions(
        self,
        session_keys: Iterable[str],
    ) -> AsyncIterator[None]:
        """Cancel/drain direct tasks, then hold their admission fences."""

        keys = tuple(
            sorted(
                {
                    canonicalize_session_key(session_key)
                    for session_key in session_keys
                }
            )
        )
        if not keys:
            yield
            return

        while True:
            tasks = {
                task
                for session_key in keys
                for task in self._unsettled_tasks.get(session_key, ())
                if not task.done()
            }
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                for session_key in keys:
                    unsettled = self._unsettled_tasks.get(session_key)
                    if unsettled is not None:
                        completed = {task for task in unsettled if task.done()}
                        unsettled.difference_update(completed)
                        if not unsettled:
                            self._unsettled_tasks.pop(session_key, None)
            cleanup_tasks = {
                task
                for session_key in keys
                for task in self._cleanup_tasks.get(session_key, ())
                if not task.done()
            }
            if cleanup_tasks:
                await asyncio.gather(*cleanup_tasks, return_exceptions=True)

            async with contextlib.AsyncExitStack() as fences:
                for session_key in keys:
                    await fences.enter_async_context(
                        self.admission(session_key)
                    )
                if any(
                    not task.done()
                    for session_key in keys
                    for task in self._unsettled_tasks.get(session_key, ())
                ):
                    continue
                if any(
                    not task.done()
                    for session_key in keys
                    for task in self._cleanup_tasks.get(session_key, ())
                ):
                    continue
                for session_key in keys:
                    current_task = self._tasks.get(session_key)
                    if current_task is not None and current_task.done():
                        self._tasks.pop(session_key, None)
                yield
                return

    def register(
        self,
        session_key: str,
        task: asyncio.Task,
        *,
        cancel_existing: bool = True,
        terminal_cleanup: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Register a running agent task for a session.

        When ``cancel_existing`` is ``True`` (the default — ``steer`` queue mode),
        any in-flight task is cancelled before the new one is stored. Callers
        using ``queue``/``followup`` modes must pass ``cancel_existing=False``
        AND must only register when no task is currently running — otherwise
        registration is rejected. Automatically removes each task when it
        completes, including cancellation tails replaced by a newer task.
        """
        key = canonicalize_session_key(session_key)
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            if cancel_existing:
                existing.cancel()
                log.warning("agent_task.replaced", session_key=key)
            else:
                # Caller violated the contract. Refuse to orphan the live task.
                raise RuntimeError(
                    f"agent_task.register(cancel_existing=False) called while a "
                    f"task is still running for session={key!r}. "
                    f"Queue mode must wait for the current task's completion."
                )
        self._tasks[key] = task
        self._unsettled_tasks.setdefault(key, set()).add(task)

        def _on_done(t: asyncio.Task) -> None:
            if terminal_cleanup is not None:
                async def _cleanup() -> None:
                    try:
                        await terminal_cleanup()
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - task outcome remains authoritative
                        log.warning(
                            "agent_task.terminal_cleanup_failed",
                            session_key=key,
                            exc_info=True,
                        )

                cleanup_task = asyncio.create_task(_cleanup())
                self._cleanup_tasks.setdefault(key, set()).add(cleanup_task)

                def _cleanup_done(completed: asyncio.Task[None]) -> None:
                    cleanups = self._cleanup_tasks.get(key)
                    if cleanups is not None:
                        cleanups.discard(completed)
                        if not cleanups:
                            self._cleanup_tasks.pop(key, None)

                cleanup_task.add_done_callback(_cleanup_done)
            if self._tasks.get(key) is t:
                self._tasks.pop(key, None)
            unsettled = self._unsettled_tasks.get(key)
            if unsettled is not None:
                unsettled.discard(t)
                if not unsettled:
                    self._unsettled_tasks.pop(key, None)
            try:
                if t.cancelled():
                    log.info("agent_task.cancelled", session_key=key)
                elif t.exception():
                    log.error(
                        "agent_task.failed",
                        session_key=key,
                        error=str(t.exception()),
                    )
                else:
                    log.info("agent_task.completed", session_key=key)
            except BrokenPipeError:
                pass

        task.add_done_callback(_on_done)

    def cancel(self, session_key: str) -> bool:
        """Cancel the running agent task for a session.

        Returns True if a task was cancelled, False if no task was running.
        """
        key = canonicalize_session_key(session_key)
        task = self._tasks.get(key)
        if task is None or task.done():
            return False

        task.cancel()
        log.info("agent_task.cancel_requested", session_key=key)
        return True

    def get(self, session_key: str) -> asyncio.Task | None:
        """Return the tracked task for a session, if any."""
        return self._tasks.get(canonicalize_session_key(session_key))

    def is_running(self, session_key: str) -> bool:
        """Check if an agent task is currently running for a session."""
        task = self._tasks.get(canonicalize_session_key(session_key))
        return task is not None and not task.done()

    def get_all(self) -> dict[str, asyncio.Task]:
        """Get all currently running agent tasks."""
        return dict(self._tasks)


# Global singleton registry
_registry: AgentTaskRegistry | None = None


def get_agent_task_registry() -> AgentTaskRegistry:
    """Get or create the global agent task registry."""
    global _registry
    if _registry is None:
        _registry = AgentTaskRegistry()
    return _registry
