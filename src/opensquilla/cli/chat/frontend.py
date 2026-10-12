"""Frontend-neutral chat command contracts."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from opensquilla.cli.chat.output import ChatOutputHandle

type ChatSessionRunner = Callable[..., Coroutine[Any, Any, None]]
type ChatCommandLauncher = Callable[..., None]


class ChatSessionHandle(Protocol):
    @property
    def session_key(self) -> str | None: ...

    @property
    def model(self) -> str | None: ...

    def output(self) -> ChatOutputHandle | None: ...
