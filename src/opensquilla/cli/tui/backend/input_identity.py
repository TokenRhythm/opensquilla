"""Identity attached to one submitted TUI composer value."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_CURRENT_CLIENT_MESSAGE_ID: ContextVar[str | None] = ContextVar(
    "opensquilla_tui_client_message_id",
    default=None,
)
_CURRENT_TURN_IDENTITY_SINK: ContextVar[
    Callable[[str, str], Awaitable[None]] | None
] = ContextVar("opensquilla_tui_turn_identity_sink", default=None)


def current_tui_client_message_id() -> str | None:
    return _CURRENT_CLIENT_MESSAGE_ID.get()


async def notify_tui_turn_identity(turn_id: str, client_message_id: str) -> None:
    sink = _CURRENT_TURN_IDENTITY_SINK.get()
    if sink is not None:
        await sink(turn_id, client_message_id)


@contextmanager
def tui_input_identity_scope(client_message_id: str | None) -> Iterator[None]:
    token = _CURRENT_CLIENT_MESSAGE_ID.set(client_message_id)
    try:
        yield
    finally:
        _CURRENT_CLIENT_MESSAGE_ID.reset(token)


@contextmanager
def tui_turn_identity_sink_scope(
    sink: Callable[[str, str], Awaitable[None]] | None,
) -> Iterator[None]:
    token = _CURRENT_TURN_IDENTITY_SINK.set(sink)
    try:
        yield
    finally:
        _CURRENT_TURN_IDENTITY_SINK.reset(token)


__all__ = [
    "current_tui_client_message_id",
    "notify_tui_turn_identity",
    "tui_input_identity_scope",
    "tui_turn_identity_sink_scope",
]
