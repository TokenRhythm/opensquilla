"""Shared concrete runtime primitives for session maintenance workflows."""

from __future__ import annotations

import inspect
from typing import Any

from opensquilla.memory.checkpoint import checkpoint_coverage_hash, checkpoint_turn_id
from opensquilla.session.compaction_lifecycle import (
    durable_receipt_allows_destructive_compaction,
)


class TaskScopedCancelUnsupportedError(RuntimeError):
    """The runtime cannot atomically cancel a task owned by one session."""


def _accepts_keyword_arg(func: Any, name: str) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(
        param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
    )


async def cancel_task_runtime(
    task_runtime: Any,
    *,
    session_key: str,
    task_id: str | None = None,
    source: str,
    reason: str,
) -> int:
    """Cancel exactly one task or one session without widening task identity."""

    exact_cancel = getattr(task_runtime, "cancel_exact", None) if task_id else None
    cancel = exact_cancel if callable(exact_cancel) else getattr(task_runtime, "cancel")
    kwargs: dict[str, Any] = {}
    if task_id:
        if not (
            _accepts_keyword_arg(cancel, "task_id")
            and _accepts_keyword_arg(cancel, "session_key")
        ):
            raise TaskScopedCancelUnsupportedError
        kwargs["task_id"] = task_id
        kwargs["session_key"] = session_key
    else:
        kwargs["session_key"] = session_key
    if _accepts_keyword_arg(cancel, "source"):
        kwargs["source"] = source
    if _accepts_keyword_arg(cancel, "reason"):
        kwargs["reason"] = reason
    return int(await cancel(**kwargs))


async def durable_checkpoint_covers_transcript(
    storage: Any,
    session_key: str,
    session_id: str | None,
    entries: list[Any],
) -> bool:
    """Prove that a successful durable checkpoint covers the exact transcript."""

    if not entries:
        return True
    list_receipts = getattr(storage, "list_memory_durable_receipts", None)
    if not callable(list_receipts):
        return False
    receipts = await list_receipts(
        session_key=session_key,
        session_id=session_id,
        scope="checkpoint",
        status="checkpoint_saved",
        coverage_turn_id=checkpoint_turn_id(entries),
        coverage_hash=checkpoint_coverage_hash(entries),
        coverage_entry_count=len(entries),
        limit=1,
    )
    return any(durable_receipt_allows_destructive_compaction(receipt) for receipt in receipts)


async def checkpoint_before_session_rewrite(
    manager: Any,
    storage: Any,
    session_key: str,
    session_id: str | None,
    entries: list[Any],
    *,
    expected_session_epoch: int | None = None,
    source: str,
) -> None:
    """Save the exact removed transcript before a destructive session rewrite."""
    if not entries:
        return
    if await durable_checkpoint_covers_transcript(storage, session_key, session_id, entries):
        return
    receipt = await manager.record_memory_checkpoint(
        session_key,
        entries,
        source=source,
        expected_session_id=session_id,
        expected_session_epoch=expected_session_epoch,
    )
    if not durable_receipt_allows_destructive_compaction(receipt):
        raise RuntimeError("transcript checkpoint failed")


__all__ = [
    "TaskScopedCancelUnsupportedError",
    "checkpoint_before_session_rewrite",
    "cancel_task_runtime",
    "durable_checkpoint_covers_transcript",
]
