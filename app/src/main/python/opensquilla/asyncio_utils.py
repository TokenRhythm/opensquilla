"""Small asyncio helpers for test-friendly background task spawning."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
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
