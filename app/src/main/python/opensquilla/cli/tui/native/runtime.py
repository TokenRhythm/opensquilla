"""Chat runtime adapter for the stable terminal surface."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from opensquilla.cli.tui.adapters.runtime_helpers import (
    ChatAbortTurn,
    ChatRuntimeContext,
    ChatRuntimeScope,
    classify_chat_input,
    clear_current_cancel,
    get_tui_output,
    surface_task_name,
)
from opensquilla.cli.tui.backend.contracts import (
    TuiRuntimeConfig,
    TuiRuntimeHooks,
    TuiSurface,
)
from opensquilla.cli.tui.backend.plugins import TuiPluginManager
from opensquilla.cli.tui.backend.runtime import run_tui_runtime
from opensquilla.cli.tui.backend.state import TuiRuntimeState
from opensquilla.cli.tui.native.renderer import status_markup
from opensquilla.cli.tui.plugins.router_hud import RouterHudPlugin, RouterHudSnapshot
from opensquilla.engine.commands import Surface


@dataclass
class NativeChatRuntimeContext(ChatRuntimeContext):
    """Typed stable terminal-chat adapter state with a legacy scope mirror."""


async def run_native_chat_runtime(
    *,
    surface: Surface,
    scope: ChatRuntimeScope,
    dispatch: Callable[[str], Awaitable[bool]],
    queue_max_size: int,
    surface_factory: Callable[[], AbstractAsyncContextManager[TuiSurface]],
    abort_active_turn: ChatAbortTurn | None = None,
    steer_active_turn: Callable[[str], Awaitable[bool]] | None = None,
) -> None:
    """Compose a Python-native terminal surface with the TUI backend runtime."""
    # Strong references to in-flight notice writes: the event loop only holds
    # weak references to scheduled tasks, and exit-path notices (Goodbye,
    # discarded-queue warnings) are scheduled with no further await before
    # teardown, so they are drained in the finally block below.
    notice_tasks: set[asyncio.Task[None]] = set()

    def _notice(payload: str) -> None:
        output = get_tui_output(scope)
        if output is None:
            return

        async def _write() -> None:
            with contextlib.suppress(Exception):
                await output.write_through(payload)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_write())
            return
        task = loop.create_task(_write())
        notice_tasks.add(task)
        task.add_done_callback(notice_tasks.discard)

    def _router_status(snapshot: RouterHudSnapshot) -> None:
        # The plain terminal has no toolbar, so router decisions surface as a
        # one-line status instead of the toolbar HUD the OpenTUI host renders.
        _notice(status_markup(snapshot.label, style=snapshot.style))

    context = NativeChatRuntimeContext(
        surface=surface,
        scope=scope,
        plugin_manager=TuiPluginManager([RouterHudPlugin(on_snapshot=_router_status)]),
        abort_active_turn=abort_active_turn,
    )

    runtime_state = TuiRuntimeState()
    scope["pending_input_provider"] = runtime_state
    try:
        await run_tui_runtime(
            dispatch=dispatch,
            surface_factory=surface_factory,
            config=TuiRuntimeConfig(
                task_name=surface_task_name(surface),
                queue_max_size=queue_max_size,
                concurrent_input_during_turn=False,
                classify_input=lambda user_input: classify_chat_input(
                    user_input,
                    surface=surface,
                ),
                state=runtime_state,
            ),
            hooks=TuiRuntimeHooks(
                clear_current_cancel=clear_current_cancel,
                notice=_notice,
                on_cancel_active_turn=context.abort_turn,
                on_steer_active_turn=(
                    steer_active_turn
                    if steer_active_turn is not None
                    else TuiRuntimeHooks().on_steer_active_turn
                ),
                expose_surface=context.expose_surface,
                clear_exposed_surface=context.clear_output,
            ),
        )
    finally:
        if scope.get("pending_input_provider") is runtime_state:
            scope.pop("pending_input_provider", None)
        if notice_tasks:
            # Exit-path notices are scheduled with no further await before
            # teardown; drive them here so none dies with the event loop.
            await asyncio.gather(*notice_tasks, return_exceptions=True)
