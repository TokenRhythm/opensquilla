"""Shared output contracts for chat frontends."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol, runtime_checkable


@runtime_checkable
class ChatOutputHandle(Protocol):
    """Typed output handle passed into chat streaming code."""

    @property
    def approval_surface(self) -> object: ...

    async def write_through(self, payload: str) -> None: ...

    def stream_output(self) -> AbstractAsyncContextManager[Callable[[str], None]]: ...
