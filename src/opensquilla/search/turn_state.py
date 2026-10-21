"""Provider health scoped to one agent turn."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from opensquilla.search.types import SearchProviderError


class SearchTurnState:
    """Remember an upstream challenge across search calls in the same turn."""

    def __init__(self) -> None:
        self._duckduckgo_lock = asyncio.Lock()
        self._duckduckgo_blocked = False

    @property
    def duckduckgo_blocked(self) -> bool:
        return self._duckduckgo_blocked

    @asynccontextmanager
    async def provider_attempt(self, provider_id: str) -> AsyncIterator[None]:
        if provider_id != "duckduckgo":
            yield
            return

        # Keep the attempt and its failure update atomic so concurrently queued
        # searches observe a challenge before making another upstream request.
        async with self._duckduckgo_lock:
            if self._duckduckgo_blocked:
                raise SearchProviderError(
                    "duckduckgo",
                    "blocked",
                    "DuckDuckGo was blocked earlier this turn. Do not retry DuckDuckGo "
                    "with another query; use an available search provider or fetch "
                    "a known page directly.",
                )
            try:
                yield
            except SearchProviderError as exc:
                if exc.kind == "blocked":
                    self._duckduckgo_blocked = True
                raise


current_search_turn_state: ContextVar[SearchTurnState | None] = ContextVar(
    "current_search_turn_state", default=None
)
