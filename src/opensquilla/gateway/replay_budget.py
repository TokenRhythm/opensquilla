"""Accounting for reconstructible replay caches, never an execution-state limit."""

from __future__ import annotations

import sys
from collections import OrderedDict, deque
from dataclasses import fields, is_dataclass
from typing import Any

DEFAULT_REPLAY_CACHE_BYTES = 64 * 1024 * 1024
DEFAULT_SESSION_REPLAY_CACHE_BYTES = 32 * 1024 * 1024


def retained_bytes(*roots: Any) -> int:
    """Estimate retained Python objects once, including shared/cyclic payloads.

    Call on admission to the idle cache or diagnostics, not on token delivery.
    This is object accounting, not RSS or a promise about allocator reclamation.
    """
    seen: set[int] = set()
    pending = list(roots)
    total = 0
    while pending:
        value = pending.pop()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(value)
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list | tuple | set | frozenset | deque):
            pending.extend(value)
        elif is_dataclass(value) and not isinstance(value, type):
            pending.extend(getattr(value, field.name) for field in fields(value))
    return total


class ReplayCacheBudget:
    """LRU ledger whose entries are already proven safe to reconstruct."""

    def __init__(
        self,
        total_limit: int = DEFAULT_REPLAY_CACHE_BYTES,
        session_limit: int = DEFAULT_SESSION_REPLAY_CACHE_BYTES,
    ) -> None:
        if total_limit < 0 or session_limit < 0:
            raise ValueError("Replay cache budgets must be non-negative")
        self.total_limit = total_limit
        self.session_limit = session_limit
        self.entries: OrderedDict[str, int] = OrderedDict()
        self.total_bytes = 0
        self.evictions = 0

    def discard(self, key: str) -> None:
        self.total_bytes -= self.entries.pop(key, 0)

    def touch(self, key: str) -> None:
        if key in self.entries:
            self.entries.move_to_end(key)

    def update(self, key: str, size: int) -> list[str]:
        self.discard(key)
        if size > self.session_limit or size > self.total_limit:
            self.evictions += 1
            return [key]
        self.entries[key] = size
        self.total_bytes += size
        evicted: list[str] = []
        while self.total_bytes > self.total_limit:
            stale, retained = self.entries.popitem(last=False)
            self.total_bytes -= retained
            evicted.append(stale)
            self.evictions += 1
        return evicted
