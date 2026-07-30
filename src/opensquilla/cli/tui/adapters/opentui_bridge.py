"""Typed bridge from REPL runtimes to the OpenTUI footer adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from opensquilla.cli.tui.adapters.runtime_helpers import (
    ChatRuntimeScope,
    clear_current_cancel,
)
from opensquilla.cli.tui.opentui.runtime import (
    get_tui_output,
    run_opentui_chat_runtime,
)
from opensquilla.engine.commands import Surface


async def run_concurrent_repl(
    *,
    surface: Surface,
    scope: ChatRuntimeScope,
    dispatch: Callable[[str], Awaitable[bool]],
    queue_max_size: int,
    abort_active_turn: Callable[[], Awaitable[None]] | None = None,
    steer_active_turn: Callable[[str], Awaitable[bool]] | None = None,
) -> None:
    """Run OpenTUI footer chat without exposing concrete adapters to chat_cmd."""
    await run_opentui_chat_runtime(
        surface=surface,
        scope=scope,
        dispatch=dispatch,
        queue_max_size=queue_max_size,
        abort_active_turn=abort_active_turn,
        steer_active_turn=steer_active_turn,
    )


__all__ = [
    "ChatRuntimeScope",
    "clear_current_cancel",
    "get_tui_output",
    "run_concurrent_repl",
]
