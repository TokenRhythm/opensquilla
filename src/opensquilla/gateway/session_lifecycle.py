"""Shared task-to-session lifecycle helpers for gateway runs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from opensquilla.session.models import AgentTaskStatus, SessionStatus

TERMINAL_SESSION_STATUSES = frozenset(
    {
        SessionStatus.DONE,
        SessionStatus.FAILED,
        SessionStatus.KILLED,
        SessionStatus.TIMEOUT,
        str(SessionStatus.DONE),
        str(SessionStatus.FAILED),
        str(SessionStatus.KILLED),
        str(SessionStatus.TIMEOUT),
    }
)


@dataclass(frozen=True)
class TaskLifecycleEvent:
    phase: Literal["queued", "running", "terminal"]
    session_key: str
    task_id: str
    task_status: AgentTaskStatus
    run_kind: str
    terminal_reason: str | None = None
    error_class: str | None = None
    error_message: str | None = None
    # False only when TaskRuntime had to fall back to an in-memory terminal
    # projection because the authoritative AgentTask update failed.
    terminal_persisted: bool = True
    # Durable queued owner created while settling accepted steer input. The
    # predecessor is terminal, but the session itself must remain active.
    continuation_task_id: str | None = None


TaskLifecycleListener = Callable[[TaskLifecycleEvent], Awaitable[None]]


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def session_status_for_task_status(status: AgentTaskStatus) -> SessionStatus | None:
    """Map a terminal task status onto the session lifecycle status."""

    status_map = {
        AgentTaskStatus.SUCCEEDED: SessionStatus.DONE,
        AgentTaskStatus.FAILED: SessionStatus.FAILED,
        AgentTaskStatus.CANCELLED: SessionStatus.KILLED,
        AgentTaskStatus.TIMEOUT: SessionStatus.TIMEOUT,
        AgentTaskStatus.ABANDONED: SessionStatus.FAILED,
    }
    return status_map.get(status)


async def apply_task_lifecycle_to_session(
    event: TaskLifecycleEvent,
    *,
    session_manager: Any,
) -> bool:
    """Synchronize a task lifecycle event into the session row.

    Returns True when the persisted session lifecycle or recents-visible
    activity changed.
    """

    # A terminal lifecycle callback can be emitted after TaskRuntime failed
    # to persist the authoritative AgentTask terminal row. Projecting that
    # in-memory fallback onto the session would make the session look done
    # while recovery still has to abandon the task and pause any owning Goal.
    if event.phase == "terminal" and not event.terminal_persisted:
        return False

    get_session = getattr(session_manager, "get_session", None)
    if not callable(get_session):
        return False
    try:
        node = await get_session(event.session_key)
    except Exception:
        return False
    if node is None:
        return False

    if event.phase in {"queued", "running"}:
        update = getattr(session_manager, "update", None)
        if not callable(update):
            return False
        if event.phase == "queued":
            try:
                await update(event.session_key)
            except Exception:
                return False
            return True
        if (
            getattr(node, "status", None) == SessionStatus.RUNNING
            and getattr(node, "ended_at", None) is None
            and getattr(node, "runtime_ms", None) is None
        ):
            try:
                await update(event.session_key)
            except Exception:
                return False
            return True
        try:
            await update(
                event.session_key,
                status=SessionStatus.RUNNING,
                started_at=_now_ms(),
                ended_at=None,
                runtime_ms=None,
            )
        except Exception:
            return False
        return True

    session_status = session_status_for_task_status(event.task_status)
    if session_status is None:
        return False
    if getattr(node, "status", None) in TERMINAL_SESSION_STATUSES:
        return False
    update = getattr(session_manager, "update", None)
    if not callable(update):
        return False
    if event.continuation_task_id:
        try:
            await update(
                event.session_key,
                status=SessionStatus.RUNNING,
                ended_at=None,
                runtime_ms=None,
            )
        except Exception:
            return False
        return True
    now = _now_ms()
    started_at = getattr(node, "started_at", None)
    runtime_ms = None
    if isinstance(started_at, (int, float)):
        runtime_ms = max(0, int(now - started_at))
    try:
        await update(
            event.session_key,
            status=session_status,
            ended_at=now,
            runtime_ms=runtime_ms,
        )
    except Exception:
        return False
    return True
