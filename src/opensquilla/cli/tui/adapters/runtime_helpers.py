"""Shared helpers for chat runtimes backed by TUI surfaces."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, MutableMapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from opensquilla.cli.tui.adapters.slash_policy import SlashCategory, classify
from opensquilla.cli.tui.backend.contracts import (
    TuiInputKind,
    TuiOutputHandle,
    TuiSurface,
)
from opensquilla.cli.tui.backend.output_binding import TuiOutputBinding
from opensquilla.cli.tui.backend.plugins import TuiPluginManager
from opensquilla.cli.tui.plugins.router_hud import RouterHudPlugin
from opensquilla.engine.commands import Surface

ChatRuntimeScope = MutableMapping[str, Any]
ChatAbortTurn = Callable[[], Awaitable[None]]


class TuiPluginOutputHandle:
    """Output handle wrapper that exposes the launch-scoped plugin manager."""

    def __init__(
        self,
        output_handle: TuiOutputHandle,
        *,
        plugin_manager: TuiPluginManager,
    ) -> None:
        self._output_handle = output_handle
        self.plugin_manager = plugin_manager

    @property
    def approval_surface(self) -> object:
        return self._output_handle.approval_surface

    async def write_through(self, payload: str) -> None:
        await self._output_handle.write_through(payload)

    async def send_message(self, message_type: str, payload: dict[str, object]) -> None:
        send = getattr(self._output_handle, "send_message", None)
        if send is not None:
            await send(message_type, payload)

    @property
    def supports_send_message(self) -> bool:
        """Whether the wrapped handle can actually deliver host IPC.

        This wrapper always exposes a callable ``send_message`` (it no-ops when
        the underlying handle can't send), so callers can't use ``callable(...)``
        to tell an IPC-capable OpenTUI surface from a native terminal. Check this
        instead before routing a host-only command (e.g. ``/theme``)."""
        return callable(getattr(self._output_handle, "send_message", None))

    @property
    def supports_request_approval(self) -> bool:
        """Whether the wrapped handle can run the host approval round-trip.

        Mirrors ``supports_send_message``: this wrapper's ``request_approval``
        is always callable, so approval callers must check this flag before
        preferring it over a plain console prompt."""
        return callable(getattr(self._output_handle, "request_approval", None))

    async def request_approval(self, request: dict[str, object]) -> object | None:
        requester = getattr(self._output_handle, "request_approval", None)
        if requester is None:
            return None
        response: object | None = await requester(request)
        return response

    async def present_gateway_approval(self, request: dict[str, object]) -> bool:
        presenter = getattr(self._output_handle, "present_gateway_approval", None)
        if not callable(presenter):
            return False
        return bool(await presenter(request))

    async def resolve_gateway_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        resolution: str | None = None,
    ) -> bool:
        resolver = getattr(self._output_handle, "resolve_gateway_approval", None)
        if not callable(resolver):
            return False
        return bool(
            await resolver(
                approval_id,
                approved=approved,
                resolution=resolution,
            )
        )

    def cancel_pending_approvals(self) -> None:
        cancel = getattr(self._output_handle, "cancel_pending_approvals", None)
        if callable(cancel):
            cancel()

    async def fail_pending_gateway_approvals(self) -> None:
        fail = getattr(self._output_handle, "fail_pending_gateway_approvals", None)
        if callable(fail):
            await fail()
            return
        self.cancel_pending_approvals()

    def stream_output(self) -> AbstractAsyncContextManager[Callable[[str], None]]:
        return self._output_handle.stream_output()

    def set_toolbar(self, key: str, value: object | None) -> None:
        setter = getattr(self._output_handle, "set_toolbar", None)
        if callable(setter):
            setter(key, value)

    def invalidate(self) -> None:
        invalidate = getattr(self._output_handle, "invalidate", None)
        if callable(invalidate):
            invalidate()


def default_tui_plugin_manager() -> TuiPluginManager:
    return TuiPluginManager([RouterHudPlugin()])


async def noop_abort_turn() -> None:
    return None


def clear_current_cancel() -> None:
    """Keep one Ctrl+C scoped to the active turn under asyncio.run."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return
    if task is not None and hasattr(task, "uncancel"):
        task.uncancel()


def map_slash_category(category: SlashCategory) -> TuiInputKind:
    """Map REPL slash policy into runtime-owned input kinds."""
    if category is SlashCategory.LOCAL:
        return TuiInputKind.LOCAL
    if category is SlashCategory.CONTROL:
        return TuiInputKind.CONTROL
    if category is SlashCategory.COMMAND:
        return TuiInputKind.COMMAND
    if category is SlashCategory.REQUIRE_IDLE:
        return TuiInputKind.COMMAND_REQUIRES_IDLE
    if category is SlashCategory.DESTRUCTIVE:
        return TuiInputKind.DESTRUCTIVE
    if category is SlashCategory.EXIT:
        return TuiInputKind.EXIT
    return TuiInputKind.NORMAL


def classify_chat_input(
    user_input: str,
    *,
    surface: Surface = Surface.CLI_GATEWAY,
) -> TuiInputKind:
    """Classify chat input without leaking slash policy into the runtime."""
    return map_slash_category(classify(user_input, surface=surface))


def surface_task_name(surface: Surface | str) -> str:
    """Name chat adapter tasks without putting engine surfaces in TUI contracts."""
    value = surface.value if isinstance(surface, Surface) else str(surface)
    return f"chat-turn-{value}"


def get_tui_output(scope: ChatRuntimeScope) -> TuiOutputHandle | None:
    """Return the active typed TUI output handle from a chat runtime scope."""
    return TuiOutputBinding(scope).get()


def expose_tui_output(scope: ChatRuntimeScope, output_handle: TuiOutputHandle) -> None:
    """Expose the active output handle to chat turn dispatch code."""
    TuiOutputBinding(scope).expose(output_handle)


def clear_tui_output(scope: ChatRuntimeScope) -> None:
    """Clear the active TUI output handle after the runtime exits."""
    TuiOutputBinding(scope).clear()


@dataclass
class ChatRuntimeContext:
    """Typed chat adapter state with a legacy scope mirror."""

    surface: Surface
    scope: ChatRuntimeScope
    plugin_manager: TuiPluginManager
    abort_active_turn: ChatAbortTurn | None = None

    @property
    def model(self) -> str | None:
        value = self.scope.get("model")
        return value if isinstance(value, str) else None

    @property
    def session_id(self) -> str | None:
        value = self.scope.get("session_key")
        return value if isinstance(value, str) else None

    def abort_turn(self) -> Awaitable[None]:
        if self.surface is not Surface.CLI_GATEWAY or self.abort_active_turn is None:
            return noop_abort_turn()
        return self.abort_active_turn()

    def expose_surface(self, tui_surface: TuiSurface) -> None:
        output_handle = getattr(tui_surface, "output_handle", None)
        if isinstance(output_handle, TuiOutputHandle):
            TuiOutputBinding(self.scope).expose(
                TuiPluginOutputHandle(
                    output_handle,
                    plugin_manager=self.plugin_manager,
                )
            )

    def clear_output(self) -> None:
        TuiOutputBinding(self.scope).clear()
