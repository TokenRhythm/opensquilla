"""Turn-local lazy transcript snapshot."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable


class TurnTranscriptSnapshot[T]:
    """Cache one successful transcript load until explicitly invalidated."""

    def __init__(self, loader: Callable[[], Awaitable[Iterable[T]]]) -> None:
        self._loader = loader
        self._entries: tuple[T, ...] | None = None
        self._lock = asyncio.Lock()
        self._load_count = 0
        self._generation = 0

    @property
    def load_count(self) -> int:
        """Return the number of loader invocations, including failed attempts."""

        return self._load_count

    @property
    def generation(self) -> int:
        """Return the invalidation generation for this turn-local snapshot."""

        return self._generation

    async def get_entries(self) -> tuple[T, ...]:
        """Return cached entries, loading once per successful generation."""

        while True:
            entries = self._entries
            if entries is not None:
                return entries

            async with self._lock:
                entries = self._entries
                if entries is not None:
                    return entries

                generation = self._generation
                self._load_count += 1
                loaded_entries = tuple(await self._loader())
                if generation != self._generation:
                    continue

                self._entries = loaded_entries
                return loaded_entries

    def invalidate(self) -> None:
        """Discard cached entries and advance the snapshot generation."""

        self._entries = None
        self._generation += 1
