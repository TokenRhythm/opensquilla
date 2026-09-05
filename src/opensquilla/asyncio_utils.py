"""Small asyncio helpers for test-friendly background task spawning."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from contextvars import ContextVar
from typing import Any


def create_background_task(coro: Coroutine[Any, Any, Any]) -> Any:
    """Create a background task and close unconsumed coroutines in tests."""
    try:
        task = asyncio.create_task(coro)
    except Exception:
        frame = getattr(coro, "cr_frame", None)
        if frame is not None:
            coro.close()
        raise
    frame = getattr(coro, "cr_frame", None)
    if frame is not None and not isinstance(task, asyncio.Task):
        coro.close()
    return task


def reset_contextvar_token(var: ContextVar[Any], token: Any) -> None:
    """Reset a ``ContextVar`` token, tolerating teardown from another Context.

    Async generators that outlive the task which first advanced them are
    finalized by asyncio's async-generator finalizer, which runs
    ``aclose()``/``athrow()`` in a *fresh* Context.  A scope whose ``finally``
    resets a token issued in the original Context then raises
    ``ValueError: <Token ...> was created in a different Context`` — the
    observed signature when an aborted turn's ``TurnRunner.run`` generator is
    finalized this way.  In that situation the variable in *this* Context was
    never set by us, so leaving it untouched is the correct cleanup; the
    owning Context's value is unaffected either way.
    """
    try:
        var.reset(token)
    except ValueError:
        pass

