"""Typed bridge from REPL runtimes to the stable terminal chat adapter."""

from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from opensquilla.cli.tui.adapters.runtime_helpers import (
    ChatRuntimeScope,
    clear_current_cancel,
    get_tui_output,
)
from opensquilla.cli.tui.backend.contracts import TuiSurface
from opensquilla.cli.tui.native.runtime import run_native_chat_runtime
from opensquilla.cli.ui import ACCENT_SOFT, console
from opensquilla.engine.commands import Surface


class NativeTerminalOutputHandle:
    approval_surface: Surface

    def __init__(self, *, approval_surface: Surface) -> None:
        self.approval_surface = approval_surface

    async def write_through(self, payload: str) -> None:
        # Rich markup stays enabled so backend notices (e.g. "[yellow]…[/yellow]")
        # render styled. Callers passing untrusted model/tool text must escape it
        # first; NativeStreamRenderer does this for all assistant output.
        # ``soft_wrap`` keeps Rich from word-wrapping each streamed fragment as if
        # the cursor were at column 0: mid-turn flushes land mid-line, so Rich's
        # own wraps would misalign and break paragraphs. The terminal wraps.
        console.print(payload, end="", soft_wrap=True)

    def stream_output(self) -> AbstractAsyncContextManager[Callable[[str], None]]:
        return _native_stream_output()

    def set_toolbar(self, key: str, value: object | None) -> None:
        return None

    def invalidate(self) -> None:
        return None


class NativeTerminalSurface:
    def __init__(self, *, approval_surface: Surface) -> None:
        self._eof_emitted = False
        self._cancel_callback: Callable[[], None] | None = None
        self._shutdown_callback: Callable[[], None] | None = None
        self._sigint_installed = False
        self._signal_fallback_installed = False
        self._previous_sigint_handler: Any = None
        self._output_handle = NativeTerminalOutputHandle(
            approval_surface=approval_surface,
        )

    @property
    def output_handle(self) -> NativeTerminalOutputHandle:
        return self._output_handle

    def _on_sigint(self) -> None:
        # Ctrl+C cancels the in-flight turn (the cancel callback is a no-op when no
        # turn is running). It NEVER ends the session — Ctrl+D (EOFError) owns exit.
        # Installing this loop handler also means console.input() no longer raises
        # KeyboardInterrupt at the prompt, so a stray Ctrl+C can't quit the session.
        cancel = self._cancel_callback
        if cancel is not None:
            cancel()

    def install_interrupt_handler(self) -> None:
        if self._sigint_installed or self._signal_fallback_installed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            loop.add_signal_handler(signal.SIGINT, self._on_sigint)
        except (RuntimeError, NotImplementedError, ValueError):
            # Loop-level signal handlers are unsupported here (e.g. Windows, or
            # a non-main thread). Without one, asyncio.run's default SIGINT
            # handling cancels the whole chat task, so Ctrl+C would quit the
            # session. Own the process-level handler for the session instead.
            self._install_signal_fallback(loop)
            return
        self._sigint_installed = True

    def _install_signal_fallback(self, loop: asyncio.AbstractEventLoop) -> None:
        def _handle_sigint(_signum: int, _frame: object | None) -> None:
            # Raw signal handlers run between bytecodes in the main thread;
            # defer to the loop so the cancel callback runs in normal
            # event-loop context instead of mid-iteration.
            loop.call_soon_threadsafe(self._on_sigint)

        previous = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, _handle_sigint)
        except (ValueError, OSError):
            # Not the main thread (or SIGINT unsupported): next_line's
            # KeyboardInterrupt fallback below stays the last resort.
            return
        self._previous_sigint_handler = previous
        self._signal_fallback_installed = True

    def remove_interrupt_handler(self) -> None:
        if self._signal_fallback_installed:
            self._signal_fallback_installed = False
            previous = self._previous_sigint_handler
            self._previous_sigint_handler = None
            restore = previous if previous is not None else signal.default_int_handler
            try:
                signal.signal(signal.SIGINT, restore)
            except (ValueError, OSError):
                pass
            return
        if not self._sigint_installed:
            return
        self._sigint_installed = False
        try:
            loop = asyncio.get_running_loop()
            loop.remove_signal_handler(signal.SIGINT)
        except (RuntimeError, NotImplementedError, ValueError):
            return

    async def next_line(self) -> str | None:
        if self._eof_emitted:
            return None
        while True:
            try:
                return await asyncio.to_thread(console.input, f"[bold {ACCENT_SOFT}]>[/] ")
            except EOFError:
                # Ctrl+D ends the session.
                self._eof_emitted = True
                if self._shutdown_callback is not None:
                    self._shutdown_callback()
                return None
            except KeyboardInterrupt:
                # Reached when an interrupted console read raises in the input
                # worker thread (Windows aborts the read directly): cancel any
                # in-flight turn and re-prompt instead of exiting, so Ctrl+C is
                # never an accidental quit.
                if self._cancel_callback is not None:
                    self._cancel_callback()
                continue

    def set_cancel_callback(self, cb: Callable[[], None] | None) -> None:
        self._cancel_callback = cb

    def set_shutdown_callback(self, cb: Callable[[], None] | None) -> None:
        self._shutdown_callback = cb

    def emit_eof(self) -> None:
        self._eof_emitted = True

    async def write_through(self, payload: str) -> None:
        await self._output_handle.write_through(payload)

    @property
    def redraw_callback(self) -> Callable[[], None]:
        return lambda: None


@asynccontextmanager
async def open_native_terminal_surface(
    *,
    surface: Surface,
) -> AsyncIterator[TuiSurface]:
    instance = NativeTerminalSurface(approval_surface=surface)
    # Own SIGINT for the session so Ctrl+C cancels turns instead of quitting.
    instance.install_interrupt_handler()
    try:
        yield instance
    finally:
        instance.remove_interrupt_handler()


@asynccontextmanager
async def _native_stream_output() -> AsyncIterator[Callable[[str], None]]:
    def _write(delta: str) -> None:
        if delta:
            sys.stdout.write(delta)
            sys.stdout.flush()

    yield _write


async def run_concurrent_repl(
    *,
    surface: Surface,
    scope: ChatRuntimeScope,
    dispatch: Callable[[str], Awaitable[bool]],
    queue_max_size: int,
    abort_active_turn: Callable[[], Awaitable[None]] | None = None,
    steer_active_turn: Callable[[str], Awaitable[bool]] | None = None,
) -> None:
    """Run stable terminal chat without requiring OpenTUI sidecar assets."""
    await run_native_chat_runtime(
        surface=surface,
        scope=scope,
        dispatch=dispatch,
        queue_max_size=queue_max_size,
        abort_active_turn=abort_active_turn,
        steer_active_turn=steer_active_turn,
        surface_factory=lambda: open_native_terminal_surface(surface=surface),
    )


__all__ = [
    "ChatRuntimeScope",
    "clear_current_cancel",
    "get_tui_output",
    "open_native_terminal_surface",
    "run_concurrent_repl",
]
